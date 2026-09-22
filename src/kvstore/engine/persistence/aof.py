"""Append-only file.

Record format (v2), one per line::

    <crc32 as 8 hex digits> <JSON array: [command, *args]>\\n
    3a5f9b1c ["SET","user:1","tejas"]

The CRC catches corruption anywhere in the file, not just a torn tail. v1
records (``{"command": ..., "args": [...]}``, written by kvstore 0.1/0.2)
are still read, so old data directories keep working.

The log holds the *effect* of each command (see ADR-0003), recorded after
the command ran in memory and committed before its reply is sent.

Writes are buffered: :meth:`AOFWriter.append` only fills a buffer and
:meth:`AOFWriter.commit` hands it to the OS, and then, depending on the fsync
policy:

* ``always``   -- fsync on every commit: nothing acknowledged is ever lost,
                  at the cost of a disk flush per commit;
* ``everysec`` -- a background thread fsyncs once a second: at most ~1s of
                  acknowledged writes lost on power failure (the Redis default);
* ``no``       -- the OS decides when to flush (typically within ~30s).

The engine commits once per batch of pipelined commands, so one fsync is
shared by the whole batch (group commit).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from kvstore.core.exceptions import AOFCorruptedError, CommandError
from kvstore.observability.metrics import Histogram
from kvstore.protocol.resp import RequestParser, encode_command

logger = logging.getLogger(__name__)

FsyncPolicy = Literal["always", "everysec", "no"]
ApplyFn = Callable[[str, list[Any]], object]


@dataclass(frozen=True, slots=True)
class ReplayResult:
    records: int
    truncated_bytes: int


def encode_record(command: str, args: Sequence[Any]) -> bytes:
    payload = json.dumps([command, *args], separators=(",", ":")).encode()
    return b"%08x %s\n" % (zlib.crc32(payload), payload)


def decode_record(raw: bytes) -> tuple[str, list[Any]]:
    if not raw.endswith(b"\n"):
        raise ValueError("incomplete record")
    if raw.startswith(b"{"):
        return _decode_v1(raw)
    if len(raw) < 11 or raw[8:9] != b" ":
        raise ValueError("malformed record")
    try:
        expected = int(raw[:8], 16)
    except ValueError:
        raise ValueError("malformed checksum") from None
    payload = raw[9:].rstrip(b"\r\n")
    if zlib.crc32(payload) != expected:
        raise ValueError("checksum mismatch")
    record = json.loads(payload)
    if not isinstance(record, list) or not record or not isinstance(record[0], str):
        raise ValueError("record must be a non-empty array starting with the command")
    return record[0], record[1:]


def _decode_v1(raw: bytes) -> tuple[str, list[Any]]:
    record = json.loads(raw)
    if not isinstance(record, dict):
        raise ValueError("record is not an object")
    command, args = record.get("command"), record.get("args")
    if not isinstance(command, str) or not isinstance(args, list):
        raise ValueError("record needs a string 'command' and a list 'args'")
    # v1 stored JSON values (numbers, objects); v2 values are strings.
    return command, [
        a if isinstance(a, str) else json.dumps(a, separators=(",", ":")) for a in args
    ]


class AOFWriter:
    def __init__(
        self, path: Path, fsync: FsyncPolicy = "everysec", fsync_seconds: Histogram | None = None
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.fsync_policy = fsync
        self.fsyncs = 0
        self._fsync_seconds = fsync_seconds  # observed under _io_lock
        self.background_error: OSError | None = None
        self._file = path.open("ab")
        self._dirty = False  # appended but not yet handed to the OS
        self._unsynced = False  # handed to the OS but not yet fsynced
        self._io_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if fsync == "everysec":
            self._thread = threading.Thread(
                target=self._fsync_every_second, name="aof-fsync", daemon=True
            )
            self._thread.start()

    @property
    def size_bytes(self) -> int:
        return self._file.tell()

    def append(self, command: str, args: Sequence[Any]) -> None:
        self._file.write(encode_record(command, args))
        self._dirty = True

    def commit(self) -> None:
        if not self._dirty:
            return
        self._file.flush()
        self._dirty = False
        if self.fsync_policy == "always":
            self._fsync()
        else:
            self._unsynced = True

    def close(self) -> None:
        if self._file.closed:
            return
        self.commit()
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        self._fsync()
        with self._io_lock:
            self._file.close()

    def _fsync(self) -> None:
        with self._io_lock:
            if not self._file.closed:
                started = time.perf_counter()
                os.fsync(self._file.fileno())
                self.fsyncs += 1
                if self._fsync_seconds is not None:
                    self._fsync_seconds.observe(time.perf_counter() - started)
        self._unsynced = False

    def _fsync_every_second(self) -> None:
        while not self._stop.wait(1.0):
            if not self._unsynced:
                continue
            try:
                self._fsync()
            except OSError as exc:  # surfaced on the next commit
                logger.error("background AOF fsync failed", extra={"error": str(exc)})
                self.background_error = exc


def replay_aof(path: Path, apply: ApplyFn, *, allow_truncate: bool = True) -> ReplayResult:
    """Feed every record in ``path`` to ``apply``.

    A torn final record (the process died mid-write) is cut off with a
    warning when ``allow_truncate`` -- that write was never acknowledged. A
    bad record anywhere else means real corruption and aborts startup rather
    than silently loading a partial dataset.
    """
    if not path.exists():
        return ReplayResult(records=0, truncated_bytes=0)

    records = 0
    good_offset = 0
    with path.open("rb") as f:
        lineno = 0
        while raw := f.readline():
            lineno += 1
            if not raw.strip():
                good_offset = f.tell()
                continue
            try:
                command, args = decode_record(raw)
            except ValueError as exc:
                if allow_truncate and not f.readline():  # nothing after it: a torn tail
                    break
                raise AOFCorruptedError(f"{path}:{lineno}: {exc}") from exc
            try:
                apply(command, args)
            except CommandError as exc:
                raise AOFCorruptedError(f"{path}:{lineno}: cannot replay: {exc}") from exc
            records += 1
            good_offset = f.tell()

    truncated = path.stat().st_size - good_offset
    if truncated:
        logger.warning(
            "truncating torn AOF tail",
            extra={"aof_path": str(path), "truncated_bytes": truncated},
        )
        os.truncate(path, good_offset)
    return ReplayResult(records=records, truncated_bytes=truncated)
