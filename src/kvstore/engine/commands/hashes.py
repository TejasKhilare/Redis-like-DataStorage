"""Hash commands."""

from __future__ import annotations

from typing import Any

from kvstore.core.exceptions import InvalidArgumentError, WrongArityError
from kvstore.engine.commands.registry import (
    DENYOOM,
    WRITE,
    CommandContext,
    check_int64,
    command,
    int_arg,
    key_arg,
    str_arg,
)
from kvstore.engine.datatypes import HashValue


@command("HSET", min_args=3, max_args=None, flags=[WRITE, DENYOOM])
def hset(ctx: CommandContext, args: list[Any]) -> int:
    if len(args) % 2 == 0:
        raise WrongArityError("wrong number of arguments for 'hset' command")
    key = key_arg(args[0])
    pairs = [(str_arg(args[i]), str_arg(args[i + 1])) for i in range(1, len(args), 2)]
    target = ctx.store.write_target(key, HashValue)
    added = sum(target.set(field, value) for field, value in pairs)
    ctx.propagate("HSET", *args)
    return added


@command("HSETNX", min_args=3, max_args=3, flags=[WRITE, DENYOOM])
def hsetnx(ctx: CommandContext, args: list[Any]) -> int:
    key, field, value = key_arg(args[0]), str_arg(args[1]), str_arg(args[2])
    existing = ctx.store.read(key, HashValue)
    if existing is not None and field in existing:
        return 0
    ctx.store.write_target(key, HashValue).set(field, value)
    ctx.propagate("HSET", key, field, value)
    return 1


@command("HGET", min_args=2, max_args=2)
def hget(ctx: CommandContext, args: list[Any]) -> str | None:
    target = ctx.store.read(key_arg(args[0]), HashValue)
    return None if target is None else target.get(str_arg(args[1]))


@command("HMGET", min_args=2, max_args=None)
def hmget(ctx: CommandContext, args: list[Any]) -> list[str | None]:
    target = ctx.store.read(key_arg(args[0]), HashValue)
    fields = [str_arg(field) for field in args[1:]]
    return [None if target is None else target.get(field) for field in fields]


@command("HDEL", min_args=2, max_args=None, flags=[WRITE])
def hdel(ctx: CommandContext, args: list[Any]) -> int:
    key = key_arg(args[0])
    fields = [str_arg(field) for field in args[1:]]
    target = ctx.store.read(key, HashValue)
    if target is None:
        return 0
    removed = [field for field in fields if target.delete(field)]
    if removed:
        ctx.propagate("HDEL", key, *removed)
    return len(removed)


@command("HGETALL", min_args=1, max_args=1)
def hgetall(ctx: CommandContext, args: list[Any]) -> list[str]:
    target = ctx.store.read(key_arg(args[0]), HashValue)
    if target is None:
        return []
    return [item for pair in target.items() for item in pair]


@command("HKEYS", min_args=1, max_args=1)
def hkeys(ctx: CommandContext, args: list[Any]) -> list[str]:
    target = ctx.store.read(key_arg(args[0]), HashValue)
    return [] if target is None else [field for field, _ in target.items()]


@command("HVALS", min_args=1, max_args=1)
def hvals(ctx: CommandContext, args: list[Any]) -> list[str]:
    target = ctx.store.read(key_arg(args[0]), HashValue)
    return [] if target is None else [value for _, value in target.items()]


@command("HLEN", min_args=1, max_args=1)
def hlen(ctx: CommandContext, args: list[Any]) -> int:
    target = ctx.store.read(key_arg(args[0]), HashValue)
    return 0 if target is None else len(target)


@command("HEXISTS", min_args=2, max_args=2)
def hexists(ctx: CommandContext, args: list[Any]) -> int:
    target = ctx.store.read(key_arg(args[0]), HashValue)
    return int(target is not None and str_arg(args[1]) in target)


@command("HINCRBY", min_args=3, max_args=3, flags=[WRITE, DENYOOM])
def hincrby(ctx: CommandContext, args: list[Any]) -> int:
    key, field, delta = key_arg(args[0]), str_arg(args[1]), int_arg(args[2])
    existing = ctx.store.read(key, HashValue)
    current = None if existing is None else existing.get(field)
    try:
        number = 0 if current is None else int(current)
    except ValueError:
        raise InvalidArgumentError("hash value is not an integer") from None
    result = check_int64(number + delta)
    ctx.store.write_target(key, HashValue).set(field, str(result))
    ctx.propagate("HINCRBY", key, field, delta)
    return result
