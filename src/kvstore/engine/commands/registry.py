"""The command table and helpers shared by every command module.

Each command is a handler registered with its arity, flags and key
positions -- the same metadata Redis exposes through ``COMMAND INFO``:

* the engine uses the flags (``write`` -> log to the AOF, ``denyoom`` ->
  refused when out of memory under ``noeviction``);
* the router uses the key positions to pick a shard without running the
  command, and runs ``stateless`` commands (PING, ECHO, ...) itself.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol, TypeVar

from kvstore.core.exceptions import InvalidArgumentError, WrongArityError
from kvstore.engine.store import Store

# ------------------------------------------------------------------ flags
WRITE: Final = "write"  # modifies the keyspace; recorded in the AOF
DENYOOM: Final = "denyoom"  # may add data; refused at the memory limit under noeviction
READONLY: Final = "readonly"
STATELESS: Final = "stateless"  # needs no keyspace; any node (incl. the router) can answer
ADMIN: Final = "admin"


class CommandContext(Protocol):
    """What a handler may use. :class:`~kvstore.engine.engine.Engine` implements it."""

    store: Store

    @property
    def loading(self) -> bool:
        """True while the AOF is being replayed."""

    def now(self) -> float: ...

    def propagate(self, command: str, *args: Any) -> None:
        """Record the effect of the running command in the AOF."""

    def flush_all(self) -> None: ...

    def start_rewrite(self) -> None: ...

    def save(self) -> None: ...

    def last_save_time(self) -> int: ...

    def config_values(self) -> dict[str, str]: ...

    def info_sections(self) -> dict[str, dict[str, Any]]: ...


Handler = Callable[[CommandContext, list[Any]], Any]
H = TypeVar("H", bound=Handler)


@dataclass(frozen=True, slots=True)
class KeySpec:
    """Positions of key arguments: ``args[first:last+1:step]``; ``last=-1`` means 'to the end'."""

    first: int = 0
    last: int = 0
    step: int = 1


SINGLE_KEY: Final = KeySpec()
ALL_KEYS: Final = KeySpec(last=-1)
EVERY_OTHER_KEY: Final = KeySpec(last=-1, step=2)


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    handler: Handler
    min_args: int
    max_args: int | None  # None: variadic
    flags: frozenset[str]
    key_spec: KeySpec | None

    @property
    def is_write(self) -> bool:
        return WRITE in self.flags

    @property
    def arity(self) -> int:
        """Redis convention: counts the command name; negative = 'at least'."""
        if self.max_args is not None and self.max_args == self.min_args:
            return self.min_args + 1
        return -(self.min_args + 1)

    def check_arity(self, args: Sequence[Any]) -> None:
        if len(args) < self.min_args or (self.max_args is not None and len(args) > self.max_args):
            raise WrongArityError(f"wrong number of arguments for '{self.name.lower()}' command")

    def keys(self, args: Sequence[Any]) -> list[Any]:
        if self.key_spec is None:
            return []
        spec = self.key_spec
        last = len(args) - 1 if spec.last == -1 else spec.last
        return list(args[spec.first : last + 1 : spec.step])


COMMANDS: dict[str, CommandSpec] = {}


def command(
    name: str,
    *,
    min_args: int,
    max_args: int | None,
    flags: Sequence[str] = (),
    keys: KeySpec | None = SINGLE_KEY,
) -> Callable[[H], H]:
    def register(handler: H) -> H:
        all_flags = frozenset(flags) | (frozenset() if WRITE in flags else {READONLY})
        COMMANDS[name] = CommandSpec(name, handler, min_args, max_args, all_flags, keys)
        return handler

    return register


# ------------------------------------------------------------ arg parsing
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1


def key_arg(arg: Any) -> str:
    if not isinstance(arg, str):
        raise InvalidArgumentError("key must be a string")
    return arg


def str_arg(arg: Any) -> str:
    if not isinstance(arg, str):
        raise InvalidArgumentError("value must be a string")
    return arg


def int_arg(arg: Any) -> int:
    """A 64-bit signed integer, from an int or its decimal text."""
    value: int | None = None
    if isinstance(arg, int) and not isinstance(arg, bool):
        value = arg
    elif isinstance(arg, str):
        try:
            value = int(arg)
        except ValueError:
            value = None
    if value is None or not _INT64_MIN <= value <= _INT64_MAX:
        raise InvalidArgumentError("value is not an integer or out of range")
    return value


def float_arg(arg: Any) -> float:
    try:
        value = float(arg) if isinstance(arg, str | int | float) else math.nan
    except ValueError:
        value = math.nan
    if math.isnan(value) or isinstance(arg, bool):
        raise InvalidArgumentError("value is not a valid float")
    return value


def score_bound(arg: Any) -> tuple[float, bool]:
    """A ZRANGEBYSCORE bound: ``1.5``, ``(1.5`` (exclusive), ``-inf``, ``+inf``."""
    text = str(arg)
    exclusive = text.startswith("(")
    try:
        value = float(text[1:] if exclusive else text)
    except ValueError:
        raise InvalidArgumentError("min or max is not a float") from None
    if math.isnan(value):
        raise InvalidArgumentError("min or max is not a float")
    return value, exclusive


def upper(arg: Any) -> str:
    return arg.upper() if isinstance(arg, str) else ""


def syntax_error() -> InvalidArgumentError:
    return InvalidArgumentError("syntax error")


def check_int64(value: int) -> int:
    if not _INT64_MIN <= value <= _INT64_MAX:
        raise InvalidArgumentError("increment or decrement would overflow")
    return value
