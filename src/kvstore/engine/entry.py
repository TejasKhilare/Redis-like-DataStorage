"""A stored value plus its metadata."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kvstore.engine.datatypes import Value

Clock = Callable[[], float]
"""Returns the current wall-clock time in seconds (``time.time`` in production)."""


class Entry:
    # __slots__ drops the per-instance __dict__: ~100 bytes saved per key.
    __slots__ = ("copied", "expires_at", "size", "value")

    def __init__(self, value: Value, expires_at: float | None = None) -> None:
        self.value = value
        self.expires_at = expires_at
        self.size = 0  # accounted bytes, maintained by the Store
        # The snapshot epoch this entry is already accounted for in (see SnapshotJob).
        self.copied = 0

    def is_expired(self, now: float) -> bool:
        return self.expires_at is not None and now >= self.expires_at
