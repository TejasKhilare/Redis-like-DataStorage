"""Command dispatcher: validates, executes, evicts and persists commands against the store."""

from __future__ import annotations

import os
import random
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from kvstore import __version__
from kvstore.core.exceptions import (
    CommandError,
    OutOfMemoryError,
    PersistenceError,
    UnknownCommandError,
)
from kvstore.engine.commands import COMMANDS, DENYOOM, CommandSpec
from kvstore.engine.entry import Clock
from kvstore.engine.eviction import create_eviction_policy
from kvstore.engine.persistence import FsyncPolicy, Persistence, PersistenceStats
from kvstore.engine.store import Store

_REDIS_POLICY_NAMES = {
    "lru": "allkeys-lru",
    "lfu": "allkeys-lfu",
    "random": "allkeys-random",
    "noeviction": "noeviction",
}


@dataclass(frozen=True, slots=True)
class EngineInfo:
    keys: int
    keys_with_ttl: int
    max_keys: int
    maxmemory: int
    used_memory: int
    eviction_policy: str
    expired_keys: int
    evicted_keys: int
    keyspace_hits: int
    keyspace_misses: int
    commands_processed: int
    persistence: PersistenceStats | None


class Engine:
    """One shard's datastore.

    Commands run one at a time under a single lock, so each is atomic -- the
    same guarantee Redis gets from its single-threaded event loop. In the
    server every caller is already on the event loop, so the lock is
    uncontended; it protects callers that use the engine from other threads.

    Lifecycle: ``open()`` loads the snapshot + AOF and starts appending,
    ``close()`` finishes any rewrite and closes the files. Without a
    ``data_dir`` the engine is purely in-memory and needs no ``open()``.
    """

    def __init__(
        self,
        *,
        max_keys: int = 0,
        maxmemory: int = 0,
        eviction_policy: str = "lru",
        data_dir: Path | None = None,
        aof_fsync: FsyncPolicy = "everysec",
        aof_rewrite_percentage: int = 100,
        aof_rewrite_min_bytes: int = 64 * 1024 * 1024,
        clock: Clock = time.time,
        rng: random.Random | None = None,
    ) -> None:
        rng = rng or random.Random()
        self.store = Store(
            create_eviction_policy(eviction_policy, clock=clock, rng=rng),
            max_keys=max_keys,
            maxmemory=maxmemory,
            clock=clock,
            rng=rng,
        )
        self._persistence = (
            Persistence(
                data_dir,
                fsync=aof_fsync,
                rewrite_percentage=aof_rewrite_percentage,
                rewrite_min_bytes=aof_rewrite_min_bytes,
                clock=clock,
            )
            if data_dir is not None
            else None
        )
        self._clock = clock
        self._replaying = False
        self._lock = threading.RLock()
        self._commit_depth = 0
        self._commands_processed = 0
        self._started_monotonic = time.monotonic()
        self._started_wall = int(clock())

    @property
    def persistence(self) -> Persistence | None:
        return self._persistence

    # --------------------------------------------------------- lifecycle
    def open(self) -> None:
        with self._lock:
            if self._persistence is None or self._persistence.is_open:
                return
            # While loading, the files alone decide what exists:
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
                self._persistence.load(self.store.load_record, self._apply)
            finally:
                self._replaying = False
                self.store.eviction_enabled = True
                self.store.expiry_enabled = True
            self.store.purge_expired()  # keys whose TTL ran out while we were down
            # Limits may have been lowered since the data was written.
            for victim in self.store.evict_if_needed():
                self.propagate("DEL", victim)
            self._commit()

    def close(self) -> None:
        with self._lock:
            if self._persistence is not None:
                self._persistence.close()

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

    # ---------------------------------------------------------- commands
    def execute(self, command: str, *args: Any) -> Any:
        spec = COMMANDS.get(command.upper()) if isinstance(command, str) else None
        if spec is None:
            raise UnknownCommandError(f"unknown command '{command}'")
        spec.check_arity(args)
        arg_list = list(args)
        with self._lock:
            if spec.is_write and self._persistence is not None:
                if not self._persistence.is_open:
                    raise PersistenceError("engine is not open; call open() before writing")
                self._persistence.check_writable()
            keys = [k for k in spec.keys(arg_list) if isinstance(k, str)]
            if DENYOOM in spec.flags and not self.store.eviction_policy.evicts:
                new_keys = sum(1 for k in set(keys) if self.store.peek(k) is None)
                if self.store.over_limit(extra_keys=new_keys):
                    raise OutOfMemoryError()
            result = self._run(spec, arg_list, keys)
            if spec.is_write:
                # A command never evicts its own keys (e.g. the value it just wrote).
                for victim in self.store.evict_if_needed(protect=set(keys)):
                    self.propagate("DEL", victim)
            self._commands_processed += 1
            if self._commit_depth == 0:
                self._commit()
        return result

    def _run(self, spec: CommandSpec, args: list[Any], keys: list[str]) -> Any:
        if not spec.is_write:
            return spec.handler(self, args)
        try:
            return spec.handler(self, args)
        finally:
            # Re-account memory for touched keys and drop emptied collections --
            # even if the command failed halfway.
            for key in keys:
                self.store.refresh(key)

    def _apply(self, command: str, args: list[Any]) -> None:
        """Replay one AOF record (no stats, no re-logging, no eviction)."""
        spec = COMMANDS.get(command.upper())
        if spec is None:
            raise UnknownCommandError(f"unknown command '{command}'")
        spec.check_arity(args)
        self._run(spec, args, [k for k in spec.keys(args) if isinstance(k, str)])

    @contextmanager
    def deferred_commit(self) -> Iterator[None]:
        """Group commit: commands inside share one AOF flush/fsync at exit.

        The caller must not send any reply before the block exits.
        """
        with self._lock:
            self._commit_depth += 1
            try:
                yield
            finally:
                self._commit_depth -= 1
                if self._commit_depth == 0:
                    self._commit()

    def _commit(self) -> None:
        if self._persistence is not None:
            self._persistence.commit()

    # --------------------------------------------------- CommandContext
    @property
    def loading(self) -> bool:
        return self._replaying

    def now(self) -> float:
        return self._clock()

    def propagate(self, command: str, *args: Any) -> None:
        if self._persistence is not None and self._persistence.is_open and not self._replaying:
            self._persistence.append(command, args)

    def flush_all(self) -> None:
        self.store.clear()

    def start_rewrite(self) -> None:
        """BGREWRITEAOF / BGSAVE: copy the keyspace now, serialize it in the background."""
        if self._persistence is None:
            raise CommandError("persistence is disabled on this node")
        with self._lock:
            self._persistence.check_writable()
            self._persistence.poll()  # finalize a finished job the cron hasn't picked up yet
            started = time.perf_counter()
            records = self.store.snapshot()
            pause_ms = round((time.perf_counter() - started) * 1000, 3)
            self._persistence.start_rewrite(records, pause_ms=pause_ms)

    def save(self) -> None:
        """SAVE: a rewrite that blocks until the snapshot is on disk."""
        self.start_rewrite()
        assert self._persistence is not None
        self._persistence.wait_rewrite()
        if self._persistence.last_rewrite_status != "ok":
            raise PersistenceError("snapshot failed, see server logs")

    def last_save_time(self) -> int:
        return self._persistence.last_save_time if self._persistence else self._started_wall

    def cron(self, expiry_sample_size: int = 20) -> int:
        """Periodic housekeeping (Redis's serverCron): expire keys, finish/start rewrites."""
        with self._lock:
            expired = self.store.expire_cycle(expiry_sample_size)
            if self._persistence is not None and self._persistence.is_open:
                self._persistence.poll()
                if self._persistence.should_auto_rewrite():
                    self.start_rewrite()
            return expired

    # -------------------------------------------------------------- info
    def info(self) -> EngineInfo:
        with self._lock:
            store = self.store
            return EngineInfo(
                keys=len(store),
                keys_with_ttl=store.volatile_count,
                max_keys=store.max_keys,
                maxmemory=store.maxmemory,
                used_memory=store.used_memory,
                eviction_policy=store.eviction_policy.name,
                expired_keys=store.expired_keys,
                evicted_keys=store.evicted_keys,
                keyspace_hits=store.keyspace_hits,
                keyspace_misses=store.keyspace_misses,
                commands_processed=self._commands_processed,
                persistence=self._persistence.stats() if self._persistence else None,
            )

    def config_values(self) -> dict[str, str]:
        p = self._persistence
        return {
            "maxmemory": str(self.store.maxmemory),
            "maxmemory-policy": _REDIS_POLICY_NAMES[self.store.eviction_policy.name],
            "appendonly": "yes" if p else "no",
            "appendfsync": p.fsync if p else "no",
            "auto-aof-rewrite-percentage": str(p.rewrite_percentage if p else 0),
            "auto-aof-rewrite-min-size": str(p.rewrite_min_bytes if p else 0),
            "save": "",
            "databases": "1",
        }

    def info_sections(self) -> dict[str, dict[str, Any]]:
        info = self.info()
        p = info.persistence
        sections: dict[str, dict[str, Any]] = {
            "server": {
                "redis_version": "7.2.0",  # the Redis API level we emulate
                "kvstore_version": __version__,
                "redis_mode": "standalone",
                "process_id": os.getpid(),
                "uptime_in_seconds": int(time.monotonic() - self._started_monotonic),
            },
            "memory": {
                "used_memory": info.used_memory,
                "used_memory_human": _human_bytes(info.used_memory),
                "maxmemory": info.maxmemory,
                "maxmemory_human": _human_bytes(info.maxmemory),
                "maxmemory_policy": _REDIS_POLICY_NAMES[info.eviction_policy],
                "max_keys": info.max_keys,
            },
            "persistence": {
                "loading": False,
                "aof_enabled": p is not None,
                "aof_rewrite_in_progress": bool(p and p.rewrite_in_progress),
                "aof_last_bgrewrite_status": p.last_rewrite_status if p else "ok",
                "aof_last_write_status": "err" if p and p.write_error else "ok",
                "aof_current_size": p.aof_current_size if p else 0,
                "aof_base_size": p.aof_base_size if p else 0,
                "aof_fsync": p.aof_fsync if p else "no",
                "aof_fsyncs": p.aof_fsyncs if p else 0,
                "aof_rewrites": p.rewrites_completed if p else 0,
                "aof_last_rewrite_time_ms": p.last_rewrite_duration_ms if p else None,
                "last_snapshot_pause_ms": p.last_snapshot_pause_ms if p else None,
                "rdb_last_save_time": self.last_save_time(),
            },
            "stats": {
                "total_commands_processed": info.commands_processed,
                "expired_keys": info.expired_keys,
                "evicted_keys": info.evicted_keys,
                "keyspace_hits": info.keyspace_hits,
                "keyspace_misses": info.keyspace_misses,
            },
            "keyspace": {},
        }
        if info.keys:
            sections["keyspace"]["db0"] = f"keys={info.keys},expires={info.keys_with_ttl},avg_ttl=0"
        return sections


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "K", "M", "G"):
        if value < 1024 or unit == "G":
            return f"{value:.2f}{unit}" if unit != "B" else f"{size}B"
        value /= 1024
    return f"{size}B"  # pragma: no cover
