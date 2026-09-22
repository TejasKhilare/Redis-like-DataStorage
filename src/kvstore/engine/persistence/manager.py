"""Coordinates a node's persistence files: recovery, the live AOF, and rewrites.

Files in the data directory::

    manifest.json        which files are current (the commit point)
    snapshot-<g>.snap    point-in-time base state   (optional)
    appendonly-<g>.aof   writes since that snapshot (one or more, in order)

Rewrite / BGSAVE, without fork():

1. On the event loop, atomically w.r.t. commands:
   a. copy the keyspace (``Store.snapshot()``, O(n) -- the only pause);
   b. open ``appendonly-<g+1>.aof`` and record it in the manifest *next to*
      the current files; every later write goes there.
2. A background thread serializes the copy into ``snapshot-<g+1>.snap``.
3. When it finishes, the manifest becomes ``{snapshot-<g+1>, [appendonly-<g+1>]}``
   and the old files are deleted.

A crash at any point leaves a manifest that recovers everything: before step
3 it still lists the old snapshot and *both* AOFs; after it, the new pair.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path

from kvstore.core.codec import SnapshotRecord
from kvstore.core.exceptions import CommandError, PersistenceWriteError
from kvstore.engine.entry import Clock
from kvstore.engine.persistence.aof import AOFWriter, ApplyFn, FsyncPolicy, replay_aof
from kvstore.engine.persistence.manifest import (
    LEGACY_AOF_NAME,
    MANIFEST_NAME,
    Manifest,
    aof_name,
    generation_of,
    snapshot_name,
)
from kvstore.engine.persistence.snapshot import read_snapshot, write_snapshot
from kvstore.observability.metrics import Histogram

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PersistenceStats:
    data_dir: str
    aof_fsync: str
    aof_current_size: int
    aof_base_size: int
    aof_fsyncs: int
    aof_records_loaded: int
    aof_truncated_bytes: int
    snapshot_keys_loaded: int
    load_duration_ms: float
    rewrite_in_progress: bool
    rewrites_completed: int
    rewrites_failed: int
    last_rewrite_status: str
    last_rewrite_duration_ms: float | None
    last_snapshot_pause_ms: float | None
    last_save_time: int
    write_error: str | None


@dataclass(slots=True)
class _RewriteJob:
    snapshot: str
    future: Future[int]
    started: float


class Persistence:
    def __init__(
        self,
        data_dir: Path,
        *,
        fsync: FsyncPolicy = "everysec",
        rewrite_percentage: int = 100,
        rewrite_min_bytes: int = 64 * 1024 * 1024,
        clock: Clock = time.time,
    ) -> None:
        self.data_dir = data_dir
        self.fsync = fsync
        self.rewrite_percentage = rewrite_percentage
        self.rewrite_min_bytes = rewrite_min_bytes
        self._clock = clock
        self._manifest = Manifest(snapshot=None, aofs=[aof_name(1)])
        self._writer: AOFWriter | None = None
        self._job: _RewriteJob | None = None
        self._copying: str | None = None  # AOF switched; the keyspace is still being copied
        self.write_error: str | None = None
        # stats
        self.fsync_seconds = Histogram()  # across AOF generations
        self.base_size = 0
        self.records_loaded = 0
        self.truncated_bytes = 0
        self.snapshot_keys_loaded = 0
        self.load_duration_ms = 0.0
        self.rewrites_completed = 0
        self.rewrites_failed = 0
        self.last_rewrite_status = "ok"
        self.last_rewrite_duration_ms: float | None = None
        self.last_snapshot_pause_ms: float | None = None
        self.last_save_time = int(clock())

    @property
    def is_open(self) -> bool:
        return self._writer is not None

    @property
    def rewrite_in_progress(self) -> bool:
        return self._job is not None or self._copying is not None

    # ------------------------------------------------------------ startup
    def load(self, load_record: Callable[[SnapshotRecord], None], apply: ApplyFn) -> None:
        """Rebuild state from disk, then open the current AOF for appending."""
        started = time.perf_counter()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        manifest = Manifest.load(self.data_dir)
        if manifest is None:
            # First start, or a directory written before manifests existed.
            legacy = (self.data_dir / LEGACY_AOF_NAME).exists()
            manifest = Manifest(snapshot=None, aofs=[LEGACY_AOF_NAME if legacy else aof_name(1)])
            manifest.save(self.data_dir)
        self._manifest = manifest

        if manifest.snapshot is not None:
            path = self.data_dir / manifest.snapshot
            for record in read_snapshot(path):
                load_record(record)
                self.snapshot_keys_loaded += 1
            self.base_size = path.stat().st_size

        last = len(manifest.aofs) - 1
        for i, name in enumerate(manifest.aofs):
            # Only the newest AOF can have a torn tail: older ones were closed cleanly.
            result = replay_aof(self.data_dir / name, apply, allow_truncate=i == last)
            self.records_loaded += result.records
            self.truncated_bytes += result.truncated_bytes

        self._writer = AOFWriter(self.data_dir / manifest.aofs[-1], self.fsync, self.fsync_seconds)
        self._delete_unreferenced()
        self.load_duration_ms = round((time.perf_counter() - started) * 1000, 3)

    # ------------------------------------------------------------- writes
    def append(self, payload: bytes) -> None:
        """One command, already RESP-encoded."""
        assert self._writer is not None
        if self.write_error is not None:
            return  # writes are refused; only e.g. an expiry's DEL gets here
        try:
            self._writer.append(payload)
        except OSError as exc:
            self._fail(exc)

    def commit(self) -> None:
        writer = self._writer
        if writer is None:
            return
        if self.write_error is not None:
            # Failed already. The failed bytes are still in the file buffer and
            # a flush would fail again -- on every batch, reads included, so
            # the node would stop answering entirely (found by a real disk-full
            # run: benchmarks/chaos.py). Writes get MISCONF from check_writable.
            return
        if writer.background_error is not None:
            error, writer.background_error = writer.background_error, None
            self._fail(error)
        try:
            writer.commit()
        except OSError as exc:
            self._fail(exc)

    def check_writable(self) -> None:
        """Refuse writes after an AOF write failure, like Redis's MISCONF."""
        if self.write_error is not None:
            raise PersistenceWriteError(
                f"Errors writing to the AOF file: {self.write_error}. "
                "Writes are disabled until the node is restarted."
            )

    def _fail(self, exc: OSError) -> None:
        self.write_error = str(exc)
        logger.error("AOF write failed; refusing further writes", extra={"error": str(exc)})
        raise PersistenceWriteError(f"Errors writing to the AOF file: {exc}") from exc

    # ------------------------------------------------------------ rewrite
    def start_rewrite(self, records: list[SnapshotRecord], *, pause_ms: float) -> None:
        """Steps 1b and 2 (see module docstring). ``records`` is the step-1a copy."""
        self.begin_rewrite()
        self.finish_copy(records, pause_ms=pause_ms)

    def begin_rewrite(self) -> None:
        """Step 1b: switch to a new AOF generation now, where the snapshot is taken.

        The copy may then be made incrementally; :meth:`finish_copy` hands it
        over. Until then the manifest lists the old files and the new AOF,
        which recover everything, like the rest of a rewrite.
        """
        if self._job is not None or self._copying is not None:
            raise CommandError("Background append only file rewriting already in progress")
        assert self._writer is not None
        generation = generation_of(self._manifest.aofs[-1]) + 1
        new_aof = aof_name(generation)

        self._writer.close()  # flush + fsync the old generation
        new_writer = AOFWriter(self.data_dir / new_aof, self.fsync, self.fsync_seconds)
        self._manifest = Manifest(self._manifest.snapshot, [*self._manifest.aofs, new_aof])
        self._manifest.save(self.data_dir)
        self._writer = new_writer
        self._copying = snapshot_name(generation)

    def abandon_copy(self) -> None:
        """The copy begun by :meth:`begin_rewrite` will never come (shutdown).

        Nothing to undo: the manifest lists the previous files and the new
        AOF, which recover everything -- the state a crash would leave.
        """
        self._copying = None

    def finish_copy(self, records: list[SnapshotRecord], *, pause_ms: float) -> None:
        """Step 2: write the copy to the snapshot file on a background thread."""
        snapshot, self._copying = self._copying, None
        assert snapshot is not None
        future: Future[int] = Future()
        created_at = self._clock()

        def run() -> None:
            try:
                size = write_snapshot(self.data_dir / snapshot, records, created_at=created_at)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(size)

        threading.Thread(target=run, name="snapshot-writer", daemon=True).start()
        self._job = _RewriteJob(snapshot, future, time.perf_counter())
        self.last_snapshot_pause_ms = pause_ms
        logger.info("background rewrite started", extra={"keys": len(records)})

    def poll(self) -> bool:
        """Finish a completed background rewrite. Returns True if one finished."""
        if self._job is None or not self._job.future.done():
            return False
        self._finish()
        return True

    def wait_rewrite(self) -> None:
        if self._job is not None:
            self._job.future.exception()  # blocks until done
            self._finish()

    def _finish(self) -> None:
        job, self._job = self._job, None
        assert job is not None
        duration_ms = round((time.perf_counter() - job.started) * 1000, 3)
        try:
            size = job.future.result()
        except Exception as exc:
            # Keep the manifest as is: it still lists every file needed.
            self.rewrites_failed += 1
            self.last_rewrite_status = "err"
            logger.error("background rewrite failed", extra={"error": repr(exc)})
            return
        self._manifest = Manifest(snapshot=job.snapshot, aofs=[self._manifest.aofs[-1]])
        self._manifest.save(self.data_dir)
        self._delete_unreferenced()
        self.base_size = size
        self.rewrites_completed += 1
        self.last_rewrite_status = "ok"
        self.last_rewrite_duration_ms = duration_ms
        self.last_save_time = int(self._clock())
        logger.info(
            "background rewrite finished",
            extra={"snapshot_bytes": size, "duration_ms": duration_ms},
        )

    def should_auto_rewrite(self) -> bool:
        """Redis's auto-aof-rewrite rule: AOF big enough and grown by N% over the base."""
        if (
            self._job is not None
            or self._copying is not None
            or not self.rewrite_percentage
            or self._writer is None
        ):
            return False
        size = self._writer.size_bytes
        if size < self.rewrite_min_bytes:
            return False
        return size * 100 >= self.base_size * self.rewrite_percentage

    def _delete_unreferenced(self) -> None:
        keep = self._manifest.files | {MANIFEST_NAME}
        for path in self.data_dir.iterdir():
            name = path.name
            is_ours = name.endswith((".aof", ".snap", ".tmp"))
            if is_ours and name not in keep:
                try:
                    path.unlink()
                except OSError as exc:
                    logger.warning(
                        "could not delete stale file", extra={"file": name, "error": str(exc)}
                    )

    # ----------------------------------------------------------- shutdown
    def close(self) -> None:
        self.wait_rewrite()
        if self._writer is not None:
            try:
                self._writer.close()
            except OSError:
                # After a failed write the unwritten tail was never acknowledged;
                # recovery truncates whatever part of it reached the disk.
                if self.write_error is None:
                    raise
            self._writer = None

    def stats(self) -> PersistenceStats:
        return PersistenceStats(
            data_dir=str(self.data_dir),
            aof_fsync=self.fsync,
            aof_current_size=self._writer.size_bytes if self._writer else 0,
            aof_base_size=self.base_size,
            aof_fsyncs=self._writer.fsyncs if self._writer else 0,
            aof_records_loaded=self.records_loaded,
            aof_truncated_bytes=self.truncated_bytes,
            snapshot_keys_loaded=self.snapshot_keys_loaded,
            load_duration_ms=self.load_duration_ms,
            rewrite_in_progress=self._job is not None,
            rewrites_completed=self.rewrites_completed,
            rewrites_failed=self.rewrites_failed,
            last_rewrite_status=self.last_rewrite_status,
            last_rewrite_duration_ms=self.last_rewrite_duration_ms,
            last_snapshot_pause_ms=self.last_snapshot_pause_ms,
            last_save_time=self.last_save_time,
            write_error=self.write_error,
        )
