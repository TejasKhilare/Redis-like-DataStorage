"""In-memory keyspace with lazy + active expiry and bounded size."""

from __future__ import annotations

import random
import time
from typing import Any

from kvstore.engine.entry import Clock, Entry
from kvstore.engine.eviction import EvictionPolicy
from kvstore.engine.keyset import SampleableKeySet

# Active expiry keeps sampling while more than 1/4 of a sample was expired,
# but never more than this many rounds per cycle so one cycle can't stall
# the event loop (Redis bounds the cycle by time instead).
_MAX_EXPIRY_ROUNDS = 16


class Store:
    """The keyspace.

    Invariants:
    * every key in ``_data`` is tracked by the eviction policy;
    * ``_volatile`` holds exactly the keys whose entry has a TTL.

    Every removal goes through :meth:`_remove`, which keeps all three in sync.
    """

    def __init__(
        self,
        max_keys: int,
        eviction: EvictionPolicy,
        *,
        clock: Clock = time.time,
        rng: random.Random | None = None,
    ) -> None:
        if max_keys <= 0:
            raise ValueError("max_keys must be positive")
        self.max_keys = max_keys
        # Both are switched off while the AOF is replayed (see Engine.open).
        self.eviction_enabled = True
        self.expiry_enabled = True
        self.expired_keys = 0
        self.evicted_keys = 0
        self._eviction = eviction
        self._clock = clock
        self._rng = rng or random.Random()
        self._data: dict[str, Entry] = {}
        self._volatile = SampleableKeySet()

    def __len__(self) -> int:
        return len(self._data)

    @property
    def eviction_policy(self) -> str:
        return self._eviction.name

    @property
    def volatile_count(self) -> int:
        return len(self._volatile)

    # ------------------------------------------------------------ reads
    def get(self, key: str) -> Entry | None:
        """Look a key up and mark it as recently used."""
        entry = self._lookup(key)
        if entry is not None:
            self._eviction.on_access(key)
        return entry

    def peek(self, key: str) -> Entry | None:
        """Look a key up without touching its recency (EXISTS, TTL)."""
        return self._lookup(key)

    # ----------------------------------------------------------- writes
    def set(self, key: str, value: Any) -> list[str]:
        """Store a value (clearing any TTL). Returns the keys evicted to make room."""
        existed = key in self._data
        self._data[key] = Entry(value)
        self._volatile.discard(key)
        if existed:
            self._eviction.on_access(key)
        else:
            self._eviction.on_insert(key)
        return self.evict_if_needed() if self.eviction_enabled else []

    def delete(self, key: str) -> bool:
        if self._lookup(key) is None:
            return False
        self._remove(key)
        return True

    def set_expiry(self, key: str, expires_at: float) -> bool:
        entry = self._lookup(key)
        if entry is None:
            return False
        entry.expires_at = expires_at
        self._volatile.add(key)
        return True

    def persist(self, key: str) -> bool:
        entry = self._lookup(key)
        if entry is None or entry.expires_at is None:
            return False
        entry.expires_at = None
        self._volatile.discard(key)
        return True

    # ------------------------------------------------------ maintenance
    def evict_if_needed(self) -> list[str]:
        evicted: list[str] = []
        while len(self._data) > self.max_keys:
            victim = self._eviction.victim()
            if victim is None:  # pragma: no cover - would break the invariant
                break
            self._remove(victim)
            self.evicted_keys += 1
            evicted.append(victim)
        return evicted

    def expire_cycle(self, sample_size: int) -> int:
        """Active expiry: randomly sample TTL keys and drop the expired ones.

        Lazy expiry alone would leak memory for keys that are never read again.
        """
        now = self._clock()
        total = 0
        for _ in range(_MAX_EXPIRY_ROUNDS):
            sample = self._volatile.sample(sample_size, self._rng)
            if not sample:
                break
            expired = 0
            for key in sample:
                if self._data[key].is_expired(now):
                    self._remove(key)
                    expired += 1
            self.expired_keys += expired
            total += expired
            if expired * 4 <= len(sample):
                break
        return total

    def purge_expired(self) -> int:
        """Drop every expired key; O(keys with a TTL). Used once after loading."""
        now = self._clock()
        stale = [key for key in self._volatile if self._data[key].is_expired(now)]
        for key in stale:
            self._remove(key)
        self.expired_keys += len(stale)
        return len(stale)

    # ---------------------------------------------------------- helpers
    def _lookup(self, key: str) -> Entry | None:
        """Return the live entry for ``key``, lazily deleting it if expired."""
        entry = self._data.get(key)
        if entry is None:
            return None
        if self.expiry_enabled and entry.is_expired(self._clock()):
            self._remove(key)
            self.expired_keys += 1
            return None
        return entry

    def _remove(self, key: str) -> None:
        del self._data[key]
        self._volatile.discard(key)
        self._eviction.on_remove(key)
