"""Command table. Importing this package registers every command."""

from kvstore.engine.commands import (  # noqa: F401  (imported for registration)
    hashes,
    keyspace,
    lists,
    server,
    sets,
    strings,
    zsets,
)
from kvstore.engine.commands.registry import (
    ADMIN,
    COMMANDS,
    DENYOOM,
    READONLY,
    STATELESS,
    WRITE,
    CommandContext,
    CommandSpec,
    KeySpec,
)

__all__ = [
    "ADMIN",
    "COMMANDS",
    "DENYOOM",
    "READONLY",
    "STATELESS",
    "WRITE",
    "CommandContext",
    "CommandSpec",
    "KeySpec",
]
