"""A stored value plus its metadata."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Clock = Callable[[], float]
"""Returns the current wall-clock time in seconds (``time.time`` in production)."""


class Entry:
    # __slots__ drops the per-instance __dict__: ~100 bytes saved per key.
    __slots__ = ("expires_at", "value")

    def __init__(self, value: Any, expires_at: float | None = None) -> None:
        self.value = value
        self.expires_at = expires_at

    def is_expired(self, now: float) -> bool:
        return self.expires_at is not None and now >= self.expires_at
