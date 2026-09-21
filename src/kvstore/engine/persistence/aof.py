"""Append-only file: one JSON record per line, ``{"command": ..., "args": [...]}``.

Recording happens *after* a command has been applied in memory and *before*
the reply is sent, so every acknowledged write is in the log. The log holds
the effect of a command, not the raw request: ``EXPIRE k 10`` is stored as an
absolute ``PEXPIREAT``, and evictions are stored as ``DEL``. Replaying the log
therefore rebuilds the same keyspace no matter when it runs.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kvstore.core.exceptions import AOFCorruptedError, CommandError

logger = logging.getLogger(__name__)

ApplyFn = Callable[[str, list[Any]], object]


@dataclass(frozen=True, slots=True)
class ReplayResult:
    records: int
    truncated_bytes: int


class AOFWriter:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # Kept open for the process lifetime: reopening per write costs a syscall pair.
        self._file = path.open("a", encoding="utf-8", newline="\n")

    def append(self, command: str, args: Sequence[Any]) -> None:
        record = json.dumps({"command": command, "args": list(args)}, separators=(",", ":"))
        self._file.write(record + "\n")
        # Hands the bytes to the OS page cache: survives a process crash, not a
        # power loss. Configurable fsync policies arrive in Phase 2.
        self._file.flush()

    @property
    def size_bytes(self) -> int:
        return os.fstat(self._file.fileno()).st_size

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()


def replay_aof(path: Path, apply: ApplyFn) -> ReplayResult:
    """Feed every record in ``path`` to ``apply``.

    A torn final record (the process died mid-write) is cut off with a
    warning -- that write was never acknowledged. A bad record anywhere else
    means real corruption and aborts startup rather than silently loading a
    partial dataset.
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
                command, args = _decode_record(raw)
            except ValueError as exc:
                if not f.readline():  # nothing after it: a torn tail
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


def _decode_record(raw: bytes) -> tuple[str, list[Any]]:
    if not raw.endswith(b"\n"):
        raise ValueError("incomplete record")
    record = json.loads(raw)
    if not isinstance(record, dict):
        raise ValueError("record is not an object")
    command, args = record.get("command"), record.get("args")
    if not isinstance(command, str) or not isinstance(args, list):
        raise ValueError("record needs a string 'command' and a list 'args'")
    return command, args
