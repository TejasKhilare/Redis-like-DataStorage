"""Command table.

Each command is a small handler registered with its arity, whether it
writes, and where its keys are. The key positions let the router pick a
shard for any command without running it (like Redis's ``COMMAND INFO``).
Adding a command means adding one function here -- the dispatcher, the TCP
server and the router don't change.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from kvstore.core.exceptions import InvalidArgumentError, WrongArityError
from kvstore.engine.store import Store


class CommandContext(Protocol):
    """What a handler may use. :class:`~kvstore.engine.engine.Engine` implements it."""

    store: Store

    @property
    def loading(self) -> bool:
        """True while the AOF is being replayed."""

    def now(self) -> float: ...

    def propagate(self, command: str, *args: Any) -> None:
        """Record the effect of the running command in the AOF."""


Handler = Callable[[CommandContext, list[Any]], Any]
H = TypeVar("H", bound=Handler)


@dataclass(frozen=True, slots=True)
class KeySpec:
    """Positions of key arguments: ``args[first:last+1:step]``; ``last=-1`` means 'to the end'."""

    first: int = 0
    last: int = 0
    step: int = 1


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    handler: Handler
    min_args: int
    max_args: int | None  # None: variadic
    write: bool
    key_spec: KeySpec | None

    def check_arity(self, args: Sequence[Any]) -> None:
        if len(args) < self.min_args or (self.max_args is not None and len(args) > self.max_args):
            raise WrongArityError(f"wrong number of arguments for '{self.name}' command")

    def keys(self, args: Sequence[Any]) -> list[Any]:
        if self.key_spec is None:
            return []
        spec = self.key_spec
        last = len(args) - 1 if spec.last == -1 else spec.last
        return list(args[spec.first : last + 1 : spec.step])


COMMANDS: dict[str, CommandSpec] = {}

_SINGLE_KEY = KeySpec()
_ALL_KEYS = KeySpec(last=-1)


def command(
    name: str,
    *,
    min_args: int,
    max_args: int | None,
    write: bool = False,
    keys: KeySpec | None = _SINGLE_KEY,
) -> Callable[[H], H]:
    def register(handler: H) -> H:
        COMMANDS[name] = CommandSpec(name, handler, min_args, max_args, write, keys)
        return handler

    return register


# ---------------------------------------------------------- arg parsing
def _key(arg: Any) -> str:
    if not isinstance(arg, str):
        raise InvalidArgumentError("key must be a string")
    return arg


def _int(arg: Any) -> int:
    if isinstance(arg, int) and not isinstance(arg, bool):
        return arg
    if isinstance(arg, str):
        try:
            return int(arg)
        except ValueError:
            pass
    raise InvalidArgumentError("value is not an integer or out of range")


def _to_ms(seconds: float) -> int:
    return int(seconds * 1000)


# ------------------------------------------------------------- commands
@command("PING", min_args=0, max_args=1, keys=None)
def ping(ctx: CommandContext, args: list[Any]) -> Any:
    return args[0] if args else "PONG"


@command("DBSIZE", min_args=0, max_args=0, keys=None)
def dbsize(ctx: CommandContext, args: list[Any]) -> int:
    return len(ctx.store)


@command("GET", min_args=1, max_args=1)
def get(ctx: CommandContext, args: list[Any]) -> Any:
    entry = ctx.store.get(_key(args[0]))
    return None if entry is None else entry.value


@command("SET", min_args=2, max_args=4, write=True)
def set_(ctx: CommandContext, args: list[Any]) -> str:
    """SET key value [EX seconds]"""
    key, value = _key(args[0]), args[1]
    if value is None:
        raise InvalidArgumentError("value must not be null")

    expire_at_ms: int | None = None
    if len(args) > 2:
        option = args[2]
        if len(args) != 4 or not isinstance(option, str) or option.upper() != "EX":
            raise InvalidArgumentError("syntax error, expected SET key value [EX seconds]")
        seconds = _int(args[3])
        if seconds <= 0:
            raise InvalidArgumentError("invalid expire time in 'SET' command")
        expire_at_ms = _to_ms(ctx.now()) + seconds * 1000

    evicted = ctx.store.set(key, value)
    ctx.propagate("SET", key, value)
    if expire_at_ms is not None:
        ctx.store.set_expiry(key, expire_at_ms / 1000)
        ctx.propagate("PEXPIREAT", key, expire_at_ms)
    for victim in evicted:
        ctx.propagate("DEL", victim)
    return "OK"


@command("DEL", min_args=1, max_args=None, write=True, keys=_ALL_KEYS)
def delete(ctx: CommandContext, args: list[Any]) -> int:
    keys = [_key(arg) for arg in args]
    deleted = [key for key in keys if ctx.store.delete(key)]
    if deleted:
        ctx.propagate("DEL", *deleted)
    return len(deleted)


@command("EXISTS", min_args=1, max_args=None, keys=_ALL_KEYS)
def exists(ctx: CommandContext, args: list[Any]) -> int:
    keys = [_key(arg) for arg in args]
    return sum(1 for key in keys if ctx.store.peek(key) is not None)


@command("EXPIRE", min_args=2, max_args=2, write=True)
def expire(ctx: CommandContext, args: list[Any]) -> int:
    key, seconds = _key(args[0]), _int(args[1])
    return _expire_at(ctx, key, _to_ms(ctx.now()) + seconds * 1000)


@command("PEXPIREAT", min_args=2, max_args=2, write=True)
def pexpireat(ctx: CommandContext, args: list[Any]) -> int:
    return _expire_at(ctx, _key(args[0]), _int(args[1]))


@command("EXPIREAT", min_args=2, max_args=2, write=True)
def expireat(ctx: CommandContext, args: list[Any]) -> int:
    # Also what the pre-0.2 AOF format used; kept so old logs still replay.
    return _expire_at(ctx, _key(args[0]), _int(args[1]) * 1000)


def _expire_at(ctx: CommandContext, key: str, at_ms: int) -> int:
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


@command("PERSIST", min_args=1, max_args=1, write=True)
def persist(ctx: CommandContext, args: list[Any]) -> int:
    key = _key(args[0])
    if not ctx.store.persist(key):
        return 0
    ctx.propagate("PERSIST", key)
    return 1


@command("PTTL", min_args=1, max_args=1)
def pttl(ctx: CommandContext, args: list[Any]) -> int:
    """Milliseconds to live; -2 if the key doesn't exist, -1 if it has no TTL."""
    entry = ctx.store.peek(_key(args[0]))
    if entry is None:
        return -2
    if entry.expires_at is None:
        return -1
    return max(0, round((entry.expires_at - ctx.now()) * 1000))


@command("TTL", min_args=1, max_args=1)
def ttl(ctx: CommandContext, args: list[Any]) -> int:
    ms = pttl(ctx, args)
    return ms if ms < 0 else (ms + 500) // 1000
