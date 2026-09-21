"""Exception hierarchy shared by every layer.

Each error carries a stable machine-readable ``code``. The code travels over
the wire (TCP and HTTP), so a client -- including the router talking to a
shard -- can rebuild the exact exception type on its side.
"""

from __future__ import annotations

from typing import ClassVar


class KVStoreError(Exception):
    """Base class for all kvstore errors."""

    code: ClassVar[str] = "INTERNAL_ERROR"
    _registry: ClassVar[dict[str, type[KVStoreError]]] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "code" in cls.__dict__:  # only classes that declare their own code
            KVStoreError._registry[cls.code] = cls

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.__class__.__name__)
        self.message = message or self.__class__.__name__

    @classmethod
    def from_code(cls, code: str, message: str) -> KVStoreError:
        """Rebuild an error received over the wire."""
        error_cls = cls._registry.get(code, KVStoreError)
        return error_cls(message)


KVStoreError._registry[KVStoreError.code] = KVStoreError


# --------------------------------------------------------------- commands
class CommandError(KVStoreError):
    """The client sent a command the engine refuses to run."""

    code = "COMMAND_ERROR"


class UnknownCommandError(CommandError):
    code = "UNKNOWN_COMMAND"


class WrongArityError(CommandError):
    code = "WRONG_ARITY"


class InvalidArgumentError(CommandError):
    code = "INVALID_ARGUMENT"


class KeyNotFoundError(KVStoreError):
    code = "KEY_NOT_FOUND"


# ---------------------------------------------------------------- protocol
class ProtocolError(KVStoreError):
    """A request could not be decoded."""

    code = "PROTOCOL_ERROR"


# ------------------------------------------------------------- persistence
class PersistenceError(KVStoreError):
    code = "PERSISTENCE_ERROR"


class AOFCorruptedError(PersistenceError):
    code = "AOF_CORRUPTED"


# ----------------------------------------------------------------- cluster
class ClusterError(KVStoreError):
    code = "CLUSTER_ERROR"


class NodeUnavailableError(ClusterError):
    """A node could not be reached or did not answer in time."""

    code = "NODE_UNAVAILABLE"


class CrossShardError(ClusterError):
    """A multi-key command touched keys owned by different shards."""

    code = "CROSS_SHARD"
