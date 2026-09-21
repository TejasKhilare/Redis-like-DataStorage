"""Generic key commands: existence, deletion, types, expiration."""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any

from kvstore.core.codec import to_bytes, to_str
from kvstore.core.exceptions import CommandError
from kvstore.engine.commands.registry import (
    ALL_KEYS,
    DENYOOM,
    WRITE,
    CommandContext,
    command,
    int_arg,
    key_arg,
    syntax_error,
    upper,
)
from kvstore.engine.datatypes import type_name
from kvstore.engine.persistence.snapshot import dump_value, load_value
from kvstore.protocol.resp import OK, SimpleString


def _to_ms(seconds: float) -> int:
    return int(seconds * 1000)


@command("DEL", min_args=1, max_args=None, flags=[WRITE], keys=ALL_KEYS)
def delete(ctx: CommandContext, args: list[Any]) -> int:
    keys = [key_arg(arg) for arg in args]
    deleted = [key for key in keys if ctx.store.delete(key)]
    if deleted:
        ctx.propagate("DEL", *deleted)
    return len(deleted)


@command("UNLINK", min_args=1, max_args=None, flags=[WRITE], keys=ALL_KEYS)
def unlink(ctx: CommandContext, args: list[Any]) -> int:
    return delete(ctx, args)


@command("EXISTS", min_args=1, max_args=None, keys=ALL_KEYS)
def exists(ctx: CommandContext, args: list[Any]) -> int:
    keys = [key_arg(arg) for arg in args]
    return sum(1 for key in keys if ctx.store.peek(key) is not None)


@command("TYPE", min_args=1, max_args=1)
def type_(ctx: CommandContext, args: list[Any]) -> SimpleString:
    entry = ctx.store.peek(key_arg(args[0]))
    return SimpleString("none" if entry is None else type_name(entry.value))


@command("KEYS", min_args=1, max_args=1, keys=None)
def keys(ctx: CommandContext, args: list[Any]) -> list[str]:
    """O(n) over the whole keyspace -- a debugging tool, as in Redis."""
    pattern = str(args[0])
    return [key for key in ctx.store.iter_keys() if fnmatchcase(key, pattern)]


@command("DBSIZE", min_args=0, max_args=0, keys=None)
def dbsize(ctx: CommandContext, args: list[Any]) -> int:
    return len(ctx.store)


@command("FLUSHALL", min_args=0, max_args=1, flags=[WRITE], keys=None)
def flushall(ctx: CommandContext, args: list[Any]) -> SimpleString:
    if args and upper(args[0]) not in ("ASYNC", "SYNC"):
        raise syntax_error()
    ctx.flush_all()
    ctx.propagate("FLUSHALL")
    return OK


@command("FLUSHDB", min_args=0, max_args=1, flags=[WRITE], keys=None)
def flushdb(ctx: CommandContext, args: list[Any]) -> SimpleString:
    return flushall(ctx, args)


# ------------------------------------------------------------ expiration
@command("EXPIRE", min_args=2, max_args=2, flags=[WRITE])
def expire(ctx: CommandContext, args: list[Any]) -> int:
    return expire_at_ms(ctx, key_arg(args[0]), _to_ms(ctx.now()) + int_arg(args[1]) * 1000)


@command("PEXPIRE", min_args=2, max_args=2, flags=[WRITE])
def pexpire(ctx: CommandContext, args: list[Any]) -> int:
    return expire_at_ms(ctx, key_arg(args[0]), _to_ms(ctx.now()) + int_arg(args[1]))


@command("EXPIREAT", min_args=2, max_args=2, flags=[WRITE])
def expireat(ctx: CommandContext, args: list[Any]) -> int:
    return expire_at_ms(ctx, key_arg(args[0]), int_arg(args[1]) * 1000)


@command("PEXPIREAT", min_args=2, max_args=2, flags=[WRITE])
def pexpireat(ctx: CommandContext, args: list[Any]) -> int:
    return expire_at_ms(ctx, key_arg(args[0]), int_arg(args[1]))


def expire_at_ms(ctx: CommandContext, key: str, at_ms: int) -> int:
    if ctx.store.peek(key) is None:
        return 0
    if at_ms <= _to_ms(ctx.now()) and not ctx.loading:
        # A deadline in the past deletes the key right away (Redis semantics).
        # Not while loading: a later record (PERSIST, SET) may still revive it.
        ctx.store.delete(key)
        ctx.propagate("DEL", key)
        return 1
    ctx.store.set_expiry(key, at_ms / 1000)
    ctx.propagate("PEXPIREAT", key, at_ms)
    return 1


@command("PERSIST", min_args=1, max_args=1, flags=[WRITE])
def persist(ctx: CommandContext, args: list[Any]) -> int:
    key = key_arg(args[0])
    if not ctx.store.persist(key):
        return 0
    ctx.propagate("PERSIST", key)
    return 1


@command("PTTL", min_args=1, max_args=1)
def pttl(ctx: CommandContext, args: list[Any]) -> int:
    """Milliseconds to live; -2 if the key doesn't exist, -1 if it has no TTL."""
    entry = ctx.store.peek(key_arg(args[0]))
    if entry is None:
        return -2
    if entry.expires_at is None:
        return -1
    return max(0, round((entry.expires_at - ctx.now()) * 1000))


@command("TTL", min_args=1, max_args=1)
def ttl(ctx: CommandContext, args: list[Any]) -> int:
    ms = pttl(ctx, args)
    return ms if ms < 0 else (ms + 500) // 1000


# ------------------------------------------------------- DUMP / RESTORE
@command("DUMP", min_args=1, max_args=1)
def dump(ctx: CommandContext, args: list[Any]) -> str | None:
    """The value, serialized (with a checksum) for RESTORE on this or another node."""
    record = ctx.store.record(key_arg(args[0]))
    if record is None:
        return None
    _, kind, payload, _ = record
    return to_str(dump_value(kind, payload))


@command("RESTORE", min_args=3, max_args=5, flags=[WRITE, DENYOOM])
def restore(ctx: CommandContext, args: list[Any]) -> SimpleString:
    """``RESTORE key ttl-ms payload [REPLACE] [ABSTTL]``; a ttl of 0 means no expiry."""
    key, ttl, raw = key_arg(args[0]), int_arg(args[1]), args[2]
    options = {upper(arg) for arg in args[3:]}
    if not options <= {"REPLACE", "ABSTTL"} or ttl < 0:
        raise syntax_error()
    if "REPLACE" not in options and ctx.store.peek(key) is not None:
        raise CommandError("Target key name already exists.", prefix="BUSYKEY")
    try:
        kind, payload = load_value(to_bytes(str(raw)))
    except ValueError as exc:
        raise CommandError(str(exc)) from None
    expires_ms = _deadline_ms(ttl, absolute="ABSTTL" in options, now_ms=_to_ms(ctx.now()))
    if expires_ms is not None and expires_ms <= _to_ms(ctx.now()) and not ctx.loading:
        ctx.store.delete(key)  # already expired: like Redis, nothing to create
        ctx.propagate("DEL", key)
        return OK
    ctx.store.delete(key)
    ctx.store.load_record((key, kind, payload, None if expires_ms is None else expires_ms / 1000))
    # Logged with an absolute deadline, so replaying it later gives the same result.
    ctx.propagate("RESTORE", key, expires_ms or 0, raw, "REPLACE", "ABSTTL")
    return OK


def _deadline_ms(ttl: int, *, absolute: bool, now_ms: int) -> int | None:
    """RESTORE's ttl as an absolute unix time in ms (``None``: no expiry)."""
    if ttl == 0:
        return None
    return ttl if absolute else now_ms + ttl
