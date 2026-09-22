"""Command dispatcher: validates, executes, evicts and persists commands against the store."""

from __future__ import annotations

import os
import random
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self

from kvstore import __version__
from kvstore.core.codec import SnapshotRecord
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
from kvstore.observability import gcpolicy
from kvstore.protocol.resp import encode_command

_REDIS_POLICY_NAMES = {
    "lru": "allkeys-lru",
    "lfu": "allkeys-lfu",
    "random": "allkeys-random",
    "noeviction": "noeviction",
}


class ReplicationFeed(Protocol):
    """Where the effects of writes go besides the AOF: the node's replication stream."""

    @property
    def active(self) -> bool:
        """Whether anything is being fed (no replica ever connected: skip the encoding)."""

    def feed(self, payload: bytes) -> None:
        """Record one effect, RESP-encoded (the same bytes as the AOF record's payload)."""

    def flush(self) -> None:
        """Called at every commit: send what was fed to the replicas."""


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
        # Copy the keyspace in slices (driven by run_cron) instead of in one
        # pause. Off by default: without a driver, a copy would never finish.
        self.incremental_snapshots = False
        # A slice copies chunks of keys until its time budget is spent: copying
        # cost depends on the values (a 10-field hash costs ~13 strings).
        self.snapshot_slice_keys = 256
        self.snapshot_slice_ms = 2.0
        self._snapshot_pause_ms = 0.0
        self._gc_held = False
        # Set by the node that owns this engine (see kvstore.replication).
        self.replication: ReplicationFeed | None = None
        self.extra_info: Callable[[], dict[str, dict[str, Any]]] | None = None
        self.store.on_expire = self._expired
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
            if self.store.abandon_snapshot():
                # Stopped mid-copy: nobody will take the result, and its
                # completion would have released the GC (see begin_snapshot).
                gcpolicy.release()
                if self._persistence is not None:
                    self._persistence.abandon_copy()
            if self._persistence is not None:
                self._persistence.close()
            self._release_gc()

    def _release_gc(self) -> None:
        if self._gc_held and not (self._persistence and self._persistence.rewrite_in_progress):
            self._gc_held = False
            gcpolicy.release()

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
            if spec.is_write and self.store.snapshot_job is not None:
                for key in keys:  # copy-on-write for the snapshot being taken
                    self.store.snapshot_barrier(key)
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
            self.hold_commit()
            try:
                yield
            finally:
                self.release_commit()

    def hold_commit(self) -> None:
        """Defer AOF commits until the matching :meth:`release_commit`.

        The split form of :meth:`deferred_commit`, for a hold that spans
        several connections' batches (:class:`~kvstore.protocol.tcp_server.GroupCommit`).
        """
        with self._lock:
            self._commit_depth += 1

    def release_commit(self) -> None:
        """End a hold; the last one out commits everything written meanwhile."""
        with self._lock:
            self._commit_depth -= 1
            if self._commit_depth == 0:
                self._commit()

    def _commit(self) -> None:
        try:
            if self._persistence is not None:
                self._persistence.commit()
        finally:
            # Replicas mirror the primary's memory, which already holds these
            # writes even if the AOF commit failed.
            if self.replication is not None:
                self.replication.flush()

    # --------------------------------------------------- CommandContext
    @property
    def loading(self) -> bool:
        return self._replaying

    def now(self) -> float:
        return self._clock()

    def propagate(self, command: str, *args: Any) -> None:
        if self._replaying:
            return
        persistence = self._persistence if self._persistence and self._persistence.is_open else None
        replication = self.replication if self.replication and self.replication.active else None
        if persistence is None and replication is None:
            return
        # Encoded once, for the AOF and for the replicas alike.
        payload = encode_command([command, *args])
        if persistence is not None:
            persistence.append(payload)
        if replication is not None:
            replication.feed(payload)

    def _expired(self, key: str) -> None:
        # An expiry is a write like any other: logged, and sent to replicas,
        # which never expire keys on their own clock.
        self.propagate("DEL", key)

    # ----------------------------------------------------------- replica
    def apply_replicated(self, command: str, args: list[Any]) -> None:
        """Run a command from the primary's replication stream.

        Unlike :meth:`execute`: no stats, no eviction or OOM check (the
        primary already decided), and no expiry while it runs -- the stream
        says exactly which keys exist, so a key the replica's clock considers
        expired must still be found. Effects still reach this node's own AOF.
        """
        spec = COMMANDS.get(command.upper())
        if spec is None:
            raise UnknownCommandError(f"unknown command '{command}'")
        spec.check_arity(args)
        with self._lock:
            keys = [k for k in spec.keys(args) if isinstance(k, str)]
            if self.store.snapshot_job is not None:
                for key in keys:
                    self.store.snapshot_barrier(key)
            expiry, self.store.expiry_enabled = self.store.expiry_enabled, False
            try:
                self._run(spec, args, keys)
            finally:
                self.store.expiry_enabled = expiry
            if self._commit_depth == 0:
                self._commit()

    def load_snapshot(self, records: Iterable[SnapshotRecord]) -> int:
        """Replace the whole keyspace (a replica's full resync); returns the key count.

        With persistence on, the new state is saved at once (SAVE), so the
        files never mix the old data with the new stream.
        """
        with self._lock:
            self.store.clear()
            expiry, self.store.expiry_enabled = self.store.expiry_enabled, False
            count = 0
            try:
                for record in records:
                    self.store.load_record(record)
                    count += 1
            finally:
                self.store.expiry_enabled = expiry
            if self._persistence is not None and self._persistence.is_open:
                self.save()
            return count

    def flush_all(self) -> None:
        self.store.clear()

    def start_rewrite(self) -> None:
        """BGREWRITEAOF / BGSAVE: copy the keyspace now, serialize it in the background."""
        if self._persistence is None:
            raise CommandError("persistence is disabled on this node")
        with self._lock:
            persistence = self._persistence
            persistence.check_writable()
            persistence.poll()  # finalize a finished job the cron hasn't picked up yet
            self._release_gc()
            if persistence.rewrite_in_progress:
                # Refused before taking a GC hold: taken twice with one flag to
                # release it, the collector stayed frozen for good.
                raise CommandError("Background append only file rewriting already in progress")
            gcpolicy.hold()  # released once the rewrite is finalized (_release_gc)
            self._gc_held = True
            try:
                if self.incremental_snapshots and self.store.snapshot_job is None:
                    persistence.begin_rewrite()

                    def write(records: list[SnapshotRecord]) -> None:
                        persistence.finish_copy(records, pause_ms=self._snapshot_pause_ms)

                    self.begin_snapshot(write)
                    return
                started = time.perf_counter()
                records = self.store.snapshot()
                pause_ms = round((time.perf_counter() - started) * 1000, 3)
                persistence.start_rewrite(records, pause_ms=pause_ms)
            except BaseException:
                self._release_gc()  # it never started: don't stay frozen
                raise

    def begin_snapshot(self, on_done: Callable[[list[SnapshotRecord]], None]) -> None:
        """Start an incremental copy of the keyspace as of now; ``on_done`` gets it.

        :meth:`step_snapshot` advances it (the shard's cron loop calls it
        between commands until it is done). CPython's collector stays frozen
        while it runs (ADR-0014): the hold is released when the copy completes,
        whoever completes it, or when :meth:`close` abandons it.
        """
        with self._lock:
            started = time.perf_counter()
            gcpolicy.hold()

            def done(records: list[SnapshotRecord]) -> None:
                gcpolicy.release()
                on_done(records)

            try:
                self.store.begin_snapshot(done)
            except BaseException:
                gcpolicy.release()
                raise
            self._snapshot_pause_ms = round((time.perf_counter() - started) * 1000, 3)

    @property
    def snapshot_in_progress(self) -> bool:
        return self.store.snapshot_job is not None

    def step_snapshot(self) -> bool:
        """Copy one slice of the running snapshot; returns whether one is still running."""
        with self._lock:
            job = self.store.snapshot_job
            if job is None:
                return False
            started = time.perf_counter()
            deadline = started + self.snapshot_slice_ms / 1000
            while True:
                done = job.step(self.snapshot_slice_keys)  # calls on_done when finished
                if done or time.perf_counter() >= deadline:
                    break
            pause = (time.perf_counter() - started) * 1000
            self._snapshot_pause_ms = round(max(self._snapshot_pause_ms, pause), 3)
            return not done

    def save(self) -> None:
        """SAVE: a rewrite that blocks until the snapshot is on disk."""
        self.start_rewrite()
        while self.step_snapshot():
            pass
        assert self._persistence is not None
        self._persistence.wait_rewrite()
        self._release_gc()
        if self._persistence.last_rewrite_status != "ok":
            raise PersistenceError("snapshot failed, see server logs")

    def last_save_time(self) -> int:
        return self._persistence.last_save_time if self._persistence else self._started_wall

    def cron(self, expiry_sample_size: int = 20) -> int:
        """Periodic housekeeping (Redis's serverCron): expire keys, finish/start rewrites."""
        with self._lock:
            expired = self.store.expire_cycle(expiry_sample_size)
            if expired and self._commit_depth == 0:
                self._commit()  # the DELs, to the AOF and the replicas
            if self._persistence is not None and self._persistence.is_open:
                self._persistence.poll()
                self._release_gc()  # whoever finished the rewrite (poll, SAVE, ...)
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
        if self.extra_info is not None:
            sections.update(self.extra_info())
        return sections


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "K", "M", "G"):
        if value < 1024 or unit == "G":
            return f"{value:.2f}{unit}" if unit != "B" else f"{size}B"
        value /= 1024
    return f"{size}B"  # pragma: no cover
