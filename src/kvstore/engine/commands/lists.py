"""List commands."""

from __future__ import annotations

from typing import Any

from kvstore.core.exceptions import InvalidArgumentError
from kvstore.engine.commands.registry import (
    DENYOOM,
    WRITE,
    CommandContext,
    command,
    int_arg,
    key_arg,
    str_arg,
)
from kvstore.engine.datatypes import ListValue
from kvstore.protocol.resp import OK, SimpleString


def _push(ctx: CommandContext, args: list[Any], *, left: bool) -> int:
    key = key_arg(args[0])
    values = [str_arg(v) for v in args[1:]]
    target = ctx.store.write_target(key, ListValue)
    if left:
        target.push_left(values)
    else:
        target.push_right(values)
    ctx.propagate("LPUSH" if left else "RPUSH", key, *values)
    return len(target)


@command("LPUSH", min_args=2, max_args=None, flags=[WRITE, DENYOOM])
def lpush(ctx: CommandContext, args: list[Any]) -> int:
    return _push(ctx, args, left=True)


@command("RPUSH", min_args=2, max_args=None, flags=[WRITE, DENYOOM])
def rpush(ctx: CommandContext, args: list[Any]) -> int:
    return _push(ctx, args, left=False)


def _pop(ctx: CommandContext, args: list[Any], *, left: bool) -> Any:
    key = key_arg(args[0])
    count: int | None = None
    if len(args) > 1:
        count = int_arg(args[1])
        if count < 0:
            raise InvalidArgumentError("value is out of range, must be positive")
    target = ctx.store.read(key, ListValue)
    if target is None:
        return None
    popped = target.pop(1 if count is None else count, left=left)
    if popped:
        ctx.propagate("LPOP" if left else "RPOP", key, len(popped))
    if count is None:
        return popped[0] if popped else None
    return popped


@command("LPOP", min_args=1, max_args=2, flags=[WRITE])
def lpop(ctx: CommandContext, args: list[Any]) -> Any:
    return _pop(ctx, args, left=True)


@command("RPOP", min_args=1, max_args=2, flags=[WRITE])
def rpop(ctx: CommandContext, args: list[Any]) -> Any:
    return _pop(ctx, args, left=False)


@command("LLEN", min_args=1, max_args=1)
def llen(ctx: CommandContext, args: list[Any]) -> int:
    target = ctx.store.read(key_arg(args[0]), ListValue)
    return 0 if target is None else len(target)


@command("LRANGE", min_args=3, max_args=3)
def lrange(ctx: CommandContext, args: list[Any]) -> list[str]:
    key, start, stop = key_arg(args[0]), int_arg(args[1]), int_arg(args[2])
    target = ctx.store.read(key, ListValue)
    return [] if target is None else target.range(start, stop)


@command("LINDEX", min_args=2, max_args=2)
def lindex(ctx: CommandContext, args: list[Any]) -> str | None:
    key, index = key_arg(args[0]), int_arg(args[1])
    target = ctx.store.read(key, ListValue)
    return None if target is None else target.get(index)


@command("LSET", min_args=3, max_args=3, flags=[WRITE, DENYOOM])
def lset(ctx: CommandContext, args: list[Any]) -> SimpleString:
    key, index, value = key_arg(args[0]), int_arg(args[1]), str_arg(args[2])
    target = ctx.store.read(key, ListValue)
    if target is None:
        raise InvalidArgumentError("no such key")
    if not target.set(index, value):
        raise InvalidArgumentError("index out of range")
    ctx.propagate("LSET", key, index, value)
    return OK


@command("LTRIM", min_args=3, max_args=3, flags=[WRITE])
def ltrim(ctx: CommandContext, args: list[Any]) -> SimpleString:
    key, start, stop = key_arg(args[0]), int_arg(args[1]), int_arg(args[2])
    target = ctx.store.read(key, ListValue)
    if target is not None:
        before = len(target)
        target.trim(start, stop)
        if len(target) != before:
            ctx.propagate("LTRIM", key, start, stop)
    return OK


@command("LREM", min_args=3, max_args=3, flags=[WRITE])
def lrem(ctx: CommandContext, args: list[Any]) -> int:
    key, count, value = key_arg(args[0]), int_arg(args[1]), str_arg(args[2])
    target = ctx.store.read(key, ListValue)
    if target is None:
        return 0
    removed = target.remove(count, value)
    if removed:
        ctx.propagate("LREM", key, count, value)
    return removed
