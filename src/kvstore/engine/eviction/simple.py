"""The trivial policies: random eviction, and no eviction at all."""

from __future__ import annotations

import random
from collections.abc import Container

from kvstore.engine.eviction.base import EvictionPolicy
from kvstore.engine.keyset import SampleableKeySet


class RandomPolicy(EvictionPolicy):
    """Evict a uniformly random key. No bookkeeping on reads at all."""

    name = "random"

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng
        self._keys = SampleableKeySet()

    def on_insert(self, key: str) -> None:
        self._keys.add(key)

    def on_access(self, key: str) -> None:
        pass

    def on_remove(self, key: str) -> None:
        self._keys.discard(key)

    def victim(self, protect: Container[str] = ()) -> str | None:
        for _ in range(8):
            picked = self._keys.sample(1, self._rng)
            if not picked:
                return None
            if picked[0] not in protect:
                return picked[0]
        return next((k for k in self._keys if k not in protect), None)

    def clear(self) -> None:
        self._keys = SampleableKeySet()

    def __len__(self) -> int:
        return len(self._keys)


class NoEvictionPolicy(EvictionPolicy):
    """Never evict: once a limit is reached, commands that add data fail with OOM."""

    name = "noeviction"
    evicts = False

    def __init__(self) -> None:
        self._count = 0

    def on_insert(self, key: str) -> None:
        self._count += 1

    def on_access(self, key: str) -> None:
        pass

    def on_remove(self, key: str) -> None:
        self._count -= 1

    def victim(self, protect: Container[str] = ()) -> str | None:
        return None

    def clear(self) -> None:
        self._count = 0

    def __len__(self) -> int:
        return self._count
