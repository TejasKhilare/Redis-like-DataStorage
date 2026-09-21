"""Connection and server commands.

``stateless`` commands don't touch the keyspace, so the router answers them
itself. They exist mostly so standard clients can connect: redis-py sends
``CLIENT SETINFO`` on connect, redis-cli asks for ``COMMAND DOCS``, and
redis-benchmark reads ``CONFIG GET save``.
"""

from __future__ import annotations

import time
from fnmatch import fnmatchcase
from typing import Any

from kvstore import __version__
from kvstore.core.exceptions import CommandError, InvalidArgumentError
from kvstore.engine.commands.registry import (
    ADMIN,
    COMMANDS,
    STATELESS,
    CommandContext,
    command,
    upper,
)
from kvstore.protocol.resp import OK, PONG, SimpleString


@command("PING", min_args=0, max_args=1, flags=[STATELESS], keys=None)
def ping(ctx: CommandContext, args: list[Any]) -> Any:
    return args[0] if args else PONG


@command("ECHO", min_args=1, max_args=1, flags=[STATELESS], keys=None)
def echo(ctx: CommandContext, args: list[Any]) -> Any:
    return args[0]


@command("TIME", min_args=0, max_args=0, flags=[STATELESS], keys=None)
def time_(ctx: CommandContext, args: list[Any]) -> list[str]:
    now_us = time.time_ns() // 1000
    return [str(now_us // 1_000_000), str(now_us % 1_000_000)]


@command("SELECT", min_args=1, max_args=1, flags=[STATELESS], keys=None)
def select(ctx: CommandContext, args: list[Any]) -> SimpleString:
    if str(args[0]) != "0":
        raise CommandError("DB index is out of range")  # one database per node
    return OK


@command("HELLO", min_args=0, max_args=None, flags=[STATELESS], keys=None)
def hello(ctx: CommandContext, args: list[Any]) -> list[Any]:
    if args and str(args[0]) != "2":
        raise CommandError("unsupported protocol version", prefix="NOPROTO")
    return [
        "server", "kvstore", "version", __version__, "proto", 2,
        "id", 1, "mode", "standalone", "role", "master", "modules", [],
    ]  # fmt: skip


@command("CLIENT", min_args=1, max_args=None, flags=[STATELESS], keys=None)
def client(ctx: CommandContext, args: list[Any]) -> Any:
    """Accepts the handshake subcommands clients send; connections carry no state yet."""
    sub = upper(args[0])
    if sub in ("SETINFO", "SETNAME"):
        return OK
    if sub == "GETNAME":
        return None
    if sub == "ID":
        return 1
    raise CommandError(f"unknown subcommand '{args[0]}'")


@command("COMMAND", min_args=0, max_args=None, flags=[STATELESS], keys=None)
def command_(ctx: CommandContext, args: list[Any]) -> Any:
    sub = upper(args[0]) if args else ""
    if sub == "COUNT":
        return len(COMMANDS)
    if sub == "DOCS":
        return []
    names = [str(a).upper() for a in args[1:]] if sub == "INFO" else sorted(COMMANDS)
    if sub == "LIST":
        return [name.lower() for name in sorted(COMMANDS)]
    if sub not in ("", "INFO"):
        raise CommandError(f"unknown subcommand '{args[0]}'")
    return [_command_info(name) if name in COMMANDS else None for name in names]


def _command_info(name: str) -> list[Any]:
    spec = COMMANDS[name]
    keys = spec.key_spec
    first, last, step = (
        (keys.first + 1, keys.last if keys.last < 0 else keys.last + 1, keys.step)
        if keys
        else (0, 0, 0)
    )
    flags = [SimpleString(flag) for flag in sorted(spec.flags)]
    return [name.lower(), spec.arity, flags, first, last, step]


@command("CONFIG", min_args=1, max_args=None, flags=[ADMIN], keys=None)
def config(ctx: CommandContext, args: list[Any]) -> Any:
    sub = upper(args[0])
    if sub == "GET" and len(args) >= 2:
        values = ctx.config_values()
        patterns = [str(p).lower() for p in args[1:]]
        return [
            item
            for name, value in values.items()
            if any(fnmatchcase(name, p) for p in patterns)
            for item in (name, value)
        ]
    if sub == "RESETSTAT":
        return OK
    raise CommandError(f"unsupported CONFIG subcommand '{args[0]}' (settings come from KV_* env)")


@command("INFO", min_args=0, max_args=None, keys=None)
def info(ctx: CommandContext, args: list[Any]) -> str:
    sections = ctx.info_sections()
    wanted = {str(a).lower() for a in args} - {"all", "everything", "default"}
    lines: list[str] = []
    for name, fields in sections.items():
        if wanted and name not in wanted:
            continue
        lines.append(f"# {name.capitalize()}")
        lines += [f"{field}:{_info_value(value)}" for field, value in fields.items()]
        lines.append("")
    return "\r\n".join(lines)


def _info_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return "" if value is None else str(value)


@command("SAVE", min_args=0, max_args=0, flags=[ADMIN], keys=None)
def save(ctx: CommandContext, args: list[Any]) -> SimpleString:
    ctx.save()
    return OK


@command("BGSAVE", min_args=0, max_args=1, flags=[ADMIN], keys=None)
def bgsave(ctx: CommandContext, args: list[Any]) -> SimpleString:
    if args and upper(args[0]) != "SCHEDULE":
        raise InvalidArgumentError("syntax error")
    ctx.start_rewrite()
    return SimpleString("Background saving started")


@command("BGREWRITEAOF", min_args=0, max_args=0, flags=[ADMIN], keys=None)
def bgrewriteaof(ctx: CommandContext, args: list[Any]) -> SimpleString:
    ctx.start_rewrite()
    return SimpleString("Background append only file rewriting started")


@command("LASTSAVE", min_args=0, max_args=0, keys=None)
def lastsave(ctx: CommandContext, args: list[Any]) -> int:
    return ctx.last_save_time()
