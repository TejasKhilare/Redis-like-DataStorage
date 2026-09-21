"""Set commands."""

from __future__ import annotations

from typing import Any

from kvstore.core.exceptions import InvalidArgumentError
from kvstore.engine.commands.registry import (
    ALL_KEYS,
    DENYOOM,
    WRITE,
    CommandContext,
    command,
    int_arg,
    key_arg,
    str_arg,
)
from kvstore.engine.datatypes import SetValue


@command("SADD", min_args=2, max_args=None, flags=[WRITE, DENYOOM])
def sadd(ctx: CommandContext, args: list[Any]) -> int:
    key = key_arg(args[0])
    members = [str_arg(m) for m in args[1:]]
    target = ctx.store.write_target(key, SetValue)
    added = [m for m in members if target.add(m)]
    if added:
        ctx.propagate("SADD", key, *added)
    return len(added)


@command("SREM", min_args=2, max_args=None, flags=[WRITE])
def srem(ctx: CommandContext, args: list[Any]) -> int:
    key = key_arg(args[0])
    members = [str_arg(m) for m in args[1:]]
    target = ctx.store.read(key, SetValue)
    if target is None:
        return 0
    removed = [m for m in members if target.remove(m)]
    if removed:
        ctx.propagate("SREM", key, *removed)
    return len(removed)


@command("SMEMBERS", min_args=1, max_args=1)
def smembers(ctx: CommandContext, args: list[Any]) -> list[str]:
    target = ctx.store.read(key_arg(args[0]), SetValue)
    return [] if target is None else list(target)


@command("SISMEMBER", min_args=2, max_args=2)
def sismember(ctx: CommandContext, args: list[Any]) -> int:
    target = ctx.store.read(key_arg(args[0]), SetValue)
    return int(target is not None and str_arg(args[1]) in target)


@command("SCARD", min_args=1, max_args=1)
def scard(ctx: CommandContext, args: list[Any]) -> int:
    target = ctx.store.read(key_arg(args[0]), SetValue)
    return 0 if target is None else len(target)


def _count(args: list[Any]) -> int | None:
    return int_arg(args[1]) if len(args) > 1 else None


@command("SPOP", min_args=1, max_args=2, flags=[WRITE])
def spop(ctx: CommandContext, args: list[Any]) -> Any:
    key, count = key_arg(args[0]), _count(args)
    if count is not None and count < 0:
        raise InvalidArgumentError("value is out of range, must be positive")
    target = ctx.store.read(key, SetValue)
    if target is None:
        return None if count is None else []
    popped = target.sample(1 if count is None else count, ctx.store.rng)
    for member in popped:
        target.remove(member)
    if popped:
        # Random choice: log *which* members went, not the SPOP (replay must be deterministic).
        ctx.propagate("SREM", key, *popped)
    if count is None:
        return popped[0] if popped else None
    return popped


@command("SRANDMEMBER", min_args=1, max_args=2)
def srandmember(ctx: CommandContext, args: list[Any]) -> Any:
    key, count = key_arg(args[0]), _count(args)
    target = ctx.store.read(key, SetValue)
    if target is None:
        return None if count is None else []
    if count is None:
        return target.sample(1, ctx.store.rng)[0]
    if count >= 0:
        return target.sample(count, ctx.store.rng)
    # Negative count: may repeat members, returns exactly |count| of them.
    members = list(target)
    return [ctx.store.rng.choice(members) for _ in range(-count)]


def _sets(ctx: CommandContext, args: list[Any]) -> list[set[str]]:
    result = []
    for arg in args:
        target = ctx.store.read(key_arg(arg), SetValue)
        result.append(set() if target is None else set(target))
    return result


@command("SINTER", min_args=1, max_args=None, keys=ALL_KEYS)
def sinter(ctx: CommandContext, args: list[Any]) -> list[str]:
    sets = sorted(_sets(ctx, args), key=len)  # start from the smallest set
    return list(set.intersection(*sets))


@command("SUNION", min_args=1, max_args=None, keys=ALL_KEYS)
def sunion(ctx: CommandContext, args: list[Any]) -> list[str]:
    return list(set.union(*_sets(ctx, args)))


@command("SDIFF", min_args=1, max_args=None, keys=ALL_KEYS)
def sdiff(ctx: CommandContext, args: list[Any]) -> list[str]:
    first, *rest = _sets(ctx, args)
    return list(first.difference(*rest))
