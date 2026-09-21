"""Exception hierarchy shared by every layer.

Each error has two stable identifiers:

* ``code`` -- used in HTTP error bodies (``{"error": {"code": ...}}``);
* ``resp_prefix`` -- the first word of a RESP error reply (``-WRONGTYPE ...``),
  exactly like Redis. Clients (including the router) rebuild the error type
  from it via :meth:`KVStoreError.from_resp`.
"""

from __future__ import annotations

from typing import ClassVar


class KVStoreError(Exception):
    """Base class for all kvstore errors."""

    code: ClassVar[str] = "INTERNAL_ERROR"
    resp_prefix: ClassVar[str] = "ERR"
    _by_prefix: ClassVar[dict[str, type[KVStoreError]]] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "resp_prefix" in cls.__dict__:  # only classes that declare their own prefix
            KVStoreError._by_prefix[cls.resp_prefix] = cls

    def __init__(self, message: str = "", *, prefix: str | None = None) -> None:
        self.message = message or self.__class__.__name__
        self.prefix = prefix or self.resp_prefix
        super().__init__(self.message)

    def to_resp(self) -> str:
        """The error line as sent over RESP (without the leading '-')."""
        return f"{self.prefix} {self.message}"

    @staticmethod
    def from_resp(text: str) -> KVStoreError:
        """Rebuild an error received as a RESP error reply."""
        prefix, _, message = text.partition(" ")
        if not prefix.isupper():
            return CommandError(text)
        error_cls = KVStoreError._by_prefix.get(prefix, CommandError)
        return error_cls(message, prefix=prefix)


# --------------------------------------------------------------- commands
class CommandError(KVStoreError):
    """The client sent a command the engine refuses to run (``-ERR ...``)."""

    code = "COMMAND_ERROR"


class UnknownCommandError(CommandError):
    code = "UNKNOWN_COMMAND"


class WrongArityError(CommandError):
    code = "WRONG_ARITY"


class InvalidArgumentError(CommandError):
    code = "INVALID_ARGUMENT"


class WrongTypeError(CommandError):
    code = "WRONG_TYPE"
    resp_prefix = "WRONGTYPE"

    def __init__(self, message: str = "", *, prefix: str | None = None) -> None:
        super().__init__(
            message or "Operation against a key holding the wrong kind of value", prefix=prefix
        )


class OutOfMemoryError(CommandError):
    code = "OUT_OF_MEMORY"
    resp_prefix = "OOM"

    def __init__(self, message: str = "", *, prefix: str | None = None) -> None:
        super().__init__(
            message or "command not allowed when used memory > 'maxmemory'.", prefix=prefix
        )


class KeyNotFoundError(KVStoreError):
    code = "KEY_NOT_FOUND"


# ---------------------------------------------------------------- protocol
class ProtocolError(KVStoreError):
    """A request or reply could not be decoded."""

    code = "PROTOCOL_ERROR"

    def __init__(self, message: str = "", *, prefix: str | None = None) -> None:
        if not message.startswith("Protocol error"):
            message = f"Protocol error: {message}"
        super().__init__(message, prefix=prefix)


# ------------------------------------------------------------- persistence
class PersistenceError(KVStoreError):
    code = "PERSISTENCE_ERROR"


class PersistenceWriteError(PersistenceError):
    """Writing the AOF failed; writes are refused until it succeeds again."""

    code = "PERSISTENCE_WRITE_ERROR"
    resp_prefix = "MISCONF"


class AOFCorruptedError(PersistenceError):
    code = "AOF_CORRUPTED"


class SnapshotCorruptedError(PersistenceError):
    code = "SNAPSHOT_CORRUPTED"


# ----------------------------------------------------------------- cluster
class ClusterError(KVStoreError):
    code = "CLUSTER_ERROR"


class NodeUnavailableError(ClusterError):
    """A node could not be reached or did not answer in time."""

    code = "NODE_UNAVAILABLE"
    resp_prefix = "CLUSTERDOWN"


class ConnectFailedError(NodeUnavailableError):
    """The node could not be connected to, so the request was never sent (safe to retry)."""

    code = "NODE_UNAVAILABLE"


class CrossShardError(ClusterError):
    """A multi-key command touched keys owned by different shards."""

    code = "CROSS_SHARD"
    resp_prefix = "CROSSSLOT"


class ReadOnlyReplicaError(ClusterError):
    """A write reached a replica (or a demoted primary): retry on the current primary."""

    code = "READ_ONLY_REPLICA"
    resp_prefix = "READONLY"

    def __init__(self, message: str = "", *, prefix: str | None = None) -> None:
        super().__init__(message or "You can't write against a read only replica.", prefix=prefix)


class TryAgainError(ClusterError):
    """The key is being moved right now; retry shortly."""

    code = "TRY_AGAIN"
    resp_prefix = "TRYAGAIN"


class AskRedirectError(ClusterError):
    """The key has moved to another node during a rebalance (``-ASK <shard> <address>``)."""

    code = "ASK_REDIRECT"
    resp_prefix = "ASK"

    @property
    def target(self) -> str:
        """The address to retry at."""
        return self.message.split()[-1]


class NotEnoughReplicasError(ClusterError):
    """``min-replicas-to-write`` is not met, so the primary refuses writes."""

    code = "NOT_ENOUGH_REPLICAS"
    resp_prefix = "NOREPLICAS"

    def __init__(self, message: str = "", *, prefix: str | None = None) -> None:
        super().__init__(message or "Not enough good replicas to write.", prefix=prefix)
