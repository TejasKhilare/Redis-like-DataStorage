"""The manifest names the files that together hold a node's data.

    {"version": 1, "snapshot": "snapshot-4.snap", "aofs": ["appendonly-5.aof"]}

Recovery = load the snapshot (if any), then replay each AOF in order.
The manifest is the single commit point of a rewrite: it is replaced
atomically, so after a crash it always describes a complete, consistent set
of files (the same idea as Redis 7's multi-part AOF manifest).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from kvstore.core.exceptions import PersistenceError
from kvstore.engine.persistence.snapshot import fsync_dir

MANIFEST_NAME = "manifest.json"
LEGACY_AOF_NAME = "appendonly.aof"
_AOF_NAME = re.compile(r"appendonly-(\d+)\.aof")


def aof_name(generation: int) -> str:
    return f"appendonly-{generation}.aof"


def snapshot_name(generation: int) -> str:
    return f"snapshot-{generation}.snap"


def generation_of(aof: str) -> int:
    match = _AOF_NAME.fullmatch(aof)
    return int(match.group(1)) if match else 0  # the legacy single file is generation 0


@dataclass(frozen=True)
class Manifest:
    snapshot: str | None
    aofs: list[str] = field(default_factory=list)

    @property
    def files(self) -> set[str]:
        return {*self.aofs, *([self.snapshot] if self.snapshot else [])}

    def save(self, directory: Path) -> None:
        path = directory / MANIFEST_NAME
        tmp = path.with_name(MANIFEST_NAME + ".tmp")
        payload = {"version": 1, "snapshot": self.snapshot, "aofs": self.aofs}
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        fsync_dir(directory)

    @classmethod
    def load(cls, directory: Path) -> Manifest | None:
        path = directory / MANIFEST_NAME
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            snapshot, aofs = payload["snapshot"], payload["aofs"]
        except (ValueError, KeyError, TypeError) as exc:
            raise PersistenceError(f"{path}: unreadable manifest: {exc}") from exc
        if not isinstance(aofs, list) or not aofs or not all(isinstance(a, str) for a in aofs):
            raise PersistenceError(f"{path}: manifest must list at least one AOF")
        if snapshot is not None and not isinstance(snapshot, str):
            raise PersistenceError(f"{path}: invalid snapshot entry")
        return cls(snapshot=snapshot, aofs=aofs)
