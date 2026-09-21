"""Least-frequently-used eviction, the way Redis does it.

Each key has an 8-bit *logarithmic* access counter (a Morris counter): the
chance of incrementing it shrinks as it grows, so 255 can represent roughly
a million hits. Counters *decay* by one per ``decay_minutes`` of idleness,
so keys that were hot long ago eventually become evictable -- pure LFU would
keep them forever. New keys start at ``INIT_COUNTER`` so they aren't evicted
before they've had a chance to be read.

The victim is the lowest counter among ``samples`` random keys. Sampling
makes eviction O(samples) instead of maintaining a global priority order.
"""

from __future__ import annotations

import random
from collections.abc import Container

from kvstore.engine.entry import Clock
from kvstore.engine.eviction.base import EvictionPolicy
from kvstore.engine.keyset import SampleableKeySet

INIT_COUNTER = 5
MAX_COUNTER = 255


class LFUPolicy(EvictionPolicy):
    name = "lfu"

    def __init__(
        self,
        clock: Clock,
        rng: random.Random,
        *,
        samples: int = 10,
        log_factor: int = 10,
        decay_minutes: int = 1,
    ) -> None:
        self._clock = clock
        self._rng = rng
        self.samples = samples
        self.log_factor = log_factor
        self.decay_minutes = decay_minutes
        self._keys = SampleableKeySet()
        # key -> (counter, minute of last decrement)
        self._meta: dict[str, tuple[int, int]] = {}

    def _minutes(self) -> int:
        return int(self._clock() // 60)

    def on_insert(self, key: str) -> None:
        self._keys.add(key)
        self._meta[key] = (INIT_COUNTER, self._minutes())

    def on_access(self, key: str) -> None:
        if key not in self._meta:
            return
        counter = self._increment(self.counter(key))
        self._meta[key] = (counter, self._minutes())

    def on_remove(self, key: str) -> None:
        if self._meta.pop(key, None) is not None:
            self._keys.discard(key)

    def counter(self, key: str) -> int:
        """The key's counter after applying decay for idle time."""
        counter, last = self._meta[key]
        if self.decay_minutes > 0:
            counter -= (self._minutes() - last) // self.decay_minutes
        return max(0, counter)

    def _increment(self, counter: int) -> int:
        if counter >= MAX_COUNTER:
            return MAX_COUNTER
        base = max(0, counter - INIT_COUNTER)
        if self._rng.random() < 1.0 / (base * self.log_factor + 1):
            counter += 1
        return counter

    def victim(self, protect: Container[str] = ()) -> str | None:
        candidates = [k for k in self._keys.sample(self.samples, self._rng) if k not in protect]
        if not candidates:
            # Unlucky sample (or tiny keyspace): fall back to a full scan.
            candidates = [k for k in self._keys if k not in protect]
            if not candidates:
                return None
        return min(candidates, key=self.counter)

    def clear(self) -> None:
        self._keys = SampleableKeySet()
        self._meta.clear()

    def __len__(self) -> int:
        return len(self._meta)
