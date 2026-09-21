"""Command dispatcher: validates, executes and persists commands against the store."""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from kvstore.core.exceptions import PersistenceError, UnknownCommandError
from kvstore.engine.commands import COMMANDS
from kvstore.engine.entry import Clock
from kvstore.engine.eviction import create_eviction_policy
from kvstore.engine.persistence import AOFWriter, replay_aof
from kvstore.engine.store import Store


@dataclass(frozen=True, slots=True)
class EngineInfo:
    keys: int
    keys_with_ttl: int
    max_keys: int
    eviction_policy: str
    expired_keys: int
    evicted_keys: int
    commands_processed: int
    aof_enabled: bool
    aof_path: str | None
    aof_size_bytes: int | None
    aof_records_loaded: int
    aof_truncated_bytes: int


class Engine:
    """One shard's datastore.

    Commands run one at a time under a single lock, so each is atomic -- the
    same guarantee Redis gets from its single-threaded event loop. In the
    server every caller is already on the event loop, so the lock is
    uncontended; it protects callers that use the engine from other threads.

    Lifecycle: ``open()`` replays the AOF and starts appending to it,
    ``close()`` releases the file. Without an ``aof_path`` the engine is
    purely in-memory and needs no ``open()``.
    """

    def __init__(
        self,
        *,
        max_keys: int = 10_000,
        eviction_policy: str = "lru",
        aof_path: Path | None = None,
        clock: Clock = time.time,
        rng: random.Random | None = None,
    ) -> None:
        self.store = Store(max_keys, create_eviction_policy(eviction_policy), clock=clock, rng=rng)
        self._clock = clock
        self._aof_path = aof_path
        self._aof: AOFWriter | None = None
        self._replaying = False
        self._lock = threading.RLock()
        self._commands_processed = 0
        self._aof_records_loaded = 0
        self._aof_truncated_bytes = 0

    # --------------------------------------------------------- lifecycle
    def open(self) -> None:
        with self._lock:
            if self._aof_path is None or self._aof is not None:
                return
            # While loading, the log alone decides what exists:
            # * no eviction -- evictions are replayed from their logged DELs;
            #   evicting again would pick victims by replay order (reads
            #   aren't logged) and rebuild a different keyspace;
            # * no expiry -- a deadline that has passed since may be undone by
            #   a later record (PERSIST, SET). Redis skips expiry while loading
            #   for the same reason.
            self._replaying = True
            self.store.eviction_enabled = False
            self.store.expiry_enabled = False
            try:
                result = replay_aof(self._aof_path, self._apply)
            finally:
                self._replaying = False
                self.store.eviction_enabled = True
                self.store.expiry_enabled = True
            self._aof_records_loaded = result.records
            self._aof_truncated_bytes = result.truncated_bytes
            self._aof = AOFWriter(self._aof_path)
            self.store.purge_expired()  # keys whose TTL ran out while we were down
            # max_keys may have been lowered since the log was written.
            for victim in self.store.evict_if_needed():
                self.propagate("DEL", victim)

    def close(self) -> None:
        with self._lock:
            if self._aof is not None:
                self._aof.close()
                self._aof = None

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --------------------------------------------------- CommandContext
    @property
    def loading(self) -> bool:
        return self._replaying

    def now(self) -> float:
        return self._clock()

    def propagate(self, command: str, *args: Any) -> None:
        if self._aof is not None and not self._replaying:
            self._aof.append(command, args)

    # ---------------------------------------------------------- commands
    def execute(self, command: str, *args: Any) -> Any:
        spec = COMMANDS.get(command.upper()) if isinstance(command, str) else None
        if spec is None:
            raise UnknownCommandError(f"unknown command '{command}'")
        spec.check_arity(args)
        if spec.write and self._aof_path is not None and self._aof is None:
            raise PersistenceError("engine is not open; call open() before writing")
        with self._lock:
            result = spec.handler(self, list(args))
            self._commands_processed += 1
        return result

    def _apply(self, command: str, args: list[Any]) -> None:
        """Replay one AOF record (no stats, no re-logging)."""
        spec = COMMANDS.get(command.upper())
        if spec is None:
            raise UnknownCommandError(f"unknown command '{command}'")
        spec.check_arity(args)
        spec.handler(self, args)

    def run_expiry_cycle(self, sample_size: int = 20) -> int:
        with self._lock:
            return self.store.expire_cycle(sample_size)

    # -------------------------------------------------------------- info
    def info(self) -> EngineInfo:
        with self._lock:
            return EngineInfo(
                keys=len(self.store),
                keys_with_ttl=self.store.volatile_count,
                max_keys=self.store.max_keys,
                eviction_policy=self.store.eviction_policy,
                expired_keys=self.store.expired_keys,
                evicted_keys=self.store.evicted_keys,
                commands_processed=self._commands_processed,
                aof_enabled=self._aof_path is not None,
                aof_path=str(self._aof_path) if self._aof_path else None,
                aof_size_bytes=self._aof.size_bytes if self._aof else None,
                aof_records_loaded=self._aof_records_loaded,
                aof_truncated_bytes=self._aof_truncated_bytes,
            )
