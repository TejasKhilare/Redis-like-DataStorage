"""String commands."""

from __future__ import annotations

from typing import Any

from kvstore.core.codec import to_bytes
from kvstore.core.exceptions import InvalidArgumentError, WrongArityError
from kvstore.engine.commands.registry import (
    ALL_KEYS,
    DENYOOM,
    EVERY_OTHER_KEY,
    WRITE,
    CommandContext,
    check_int64,
    command,
    float_arg,
    int_arg,
    key_arg,
    str_arg,
    syntax_error,
    upper,
)
from kvstore.protocol.resp import OK, format_float


@command("GET", min_args=1, max_args=1)
def get(ctx: CommandContext, args: list[Any]) -> str | None:
    return ctx.store.read_str(key_arg(args[0]))


@command("SET", min_args=2, max_args=None, flags=[WRITE, DENYOOM])
def set_(ctx: CommandContext, args: list[Any]) -> Any:
    """SET key value [NX | XX] [GET] [EX seconds | PX milliseconds | KEEPTTL]"""
    key, value = key_arg(args[0]), str_arg(args[1])
    nx = xx = get_old = keep_ttl = False
    expire_ms: int | None = None
    i = 2
    while i < len(args):
        option = upper(args[i])
        if option == "NX" and not xx:
            nx = True
        elif option == "XX" and not nx:
            xx = True
        elif option == "GET":
            get_old = True
        elif option == "KEEPTTL" and expire_ms is None:
            keep_ttl = True
        elif option in ("EX", "PX") and expire_ms is None and not keep_ttl and i + 1 < len(args):
            amount = int_arg(args[i + 1])
            if amount <= 0:
                raise InvalidArgumentError("invalid expire time in 'set' command")
            expire_ms = amount * 1000 if option == "EX" else amount
            i += 1
        else:
            raise syntax_error()
        i += 1

    old = ctx.store.read_str(key) if get_old else None
    exists = ctx.store.peek(key) is not None
    if (nx and exists) or (xx and not exists):
        return old if get_old else None

    ctx.store.set(key, value, keep_ttl=keep_ttl)
    ctx.propagate("SET", key, value, *(["KEEPTTL"] if keep_ttl else []))
    if expire_ms is not None:
        at_ms = int(ctx.now() * 1000) + expire_ms
        ctx.store.set_expiry(key, at_ms / 1000)
        ctx.propagate("PEXPIREAT", key, at_ms)
    return old if get_old else OK


@command("SETNX", min_args=2, max_args=2, flags=[WRITE, DENYOOM])
def setnx(ctx: CommandContext, args: list[Any]) -> int:
    return 0 if set_(ctx, [args[0], args[1], "NX"]) is None else 1


@command("GETDEL", min_args=1, max_args=1, flags=[WRITE])
def getdel(ctx: CommandContext, args: list[Any]) -> str | None:
    key = key_arg(args[0])
    value = ctx.store.read_str(key)
    if value is not None:
        ctx.store.delete(key)
        ctx.propagate("DEL", key)
    return value


@command("MGET", min_args=1, max_args=None, keys=ALL_KEYS)
def mget(ctx: CommandContext, args: list[Any]) -> list[str | None]:
    values: list[str | None] = []
    for arg in args:
        entry = ctx.store.get(key_arg(arg))
        # Non-string values read as nil in MGET, as in Redis.
        values.append(entry.value if entry is not None and isinstance(entry.value, str) else None)
    return values


@command("MSET", min_args=2, max_args=None, flags=[WRITE, DENYOOM], keys=EVERY_OTHER_KEY)
def mset(ctx: CommandContext, args: list[Any]) -> Any:
    if len(args) % 2:
        raise WrongArityError("wrong number of arguments for 'mset' command")
    pairs = [(key_arg(args[i]), str_arg(args[i + 1])) for i in range(0, len(args), 2)]
    for key, value in pairs:
        ctx.store.set(key, value)
    ctx.propagate("MSET", *args)
    return OK


def _incr_by(ctx: CommandContext, key: str, delta: int) -> int:
    current = ctx.store.read_str(key)
    try:
        number = 0 if current is None else int(current)
    except ValueError:
        raise InvalidArgumentError("value is not an integer or out of range") from None
    result = check_int64(number + delta)
    ctx.store.set(key, str(result), keep_ttl=True)
    ctx.propagate("INCRBY", key, delta)
    return result


@command("INCR", min_args=1, max_args=1, flags=[WRITE, DENYOOM])
def incr(ctx: CommandContext, args: list[Any]) -> int:
    return _incr_by(ctx, key_arg(args[0]), 1)


@command("DECR", min_args=1, max_args=1, flags=[WRITE, DENYOOM])
def decr(ctx: CommandContext, args: list[Any]) -> int:
    return _incr_by(ctx, key_arg(args[0]), -1)


@command("INCRBY", min_args=2, max_args=2, flags=[WRITE, DENYOOM])
def incrby(ctx: CommandContext, args: list[Any]) -> int:
    return _incr_by(ctx, key_arg(args[0]), int_arg(args[1]))


@command("DECRBY", min_args=2, max_args=2, flags=[WRITE, DENYOOM])
def decrby(ctx: CommandContext, args: list[Any]) -> int:
    return _incr_by(ctx, key_arg(args[0]), -int_arg(args[1]))


@command("INCRBYFLOAT", min_args=2, max_args=2, flags=[WRITE, DENYOOM])
def incrbyfloat(ctx: CommandContext, args: list[Any]) -> str:
    key, delta = key_arg(args[0]), float_arg(args[1])
    current = ctx.store.read_str(key)
    try:
        number = 0.0 if current is None else float_arg(current)
    except InvalidArgumentError:
        raise InvalidArgumentError("value is not a valid float") from None
    result = number + delta
    if result in (float("inf"), float("-inf")):
        raise InvalidArgumentError("increment would produce NaN or Infinity")
    text = format_float(result)
    ctx.store.set(key, text, keep_ttl=True)
    # Float formatting must not differ on replay: log the resulting value.
    ctx.propagate("SET", key, text, "KEEPTTL")
    return text


@command("APPEND", min_args=2, max_args=2, flags=[WRITE, DENYOOM])
def append(ctx: CommandContext, args: list[Any]) -> int:
    key, suffix = key_arg(args[0]), str_arg(args[1])
    value = (ctx.store.read_str(key) or "") + suffix
    ctx.store.set(key, value, keep_ttl=True)
    ctx.propagate("APPEND", key, suffix)
    return len(to_bytes(value))


@command("STRLEN", min_args=1, max_args=1)
def strlen(ctx: CommandContext, args: list[Any]) -> int:
    value = ctx.store.read_str(key_arg(args[0]))
    return 0 if value is None else len(to_bytes(value))
