"""A set of keys that supports O(1) add/remove and O(k) random sampling."""

from __future__ import annotations

import random
from collections.abc import Iterator


class SampleableKeySet:
    """Keys stored in a dense array plus a key -> index map.

    Removal swaps the last element into the hole, so the array never has gaps
    and ``random.sample`` can pick from it directly. Active expiry samples from
    this set (only keys that have a TTL) instead of copying every key in the
    store on each cycle -- the same idea as Redis's separate ``expires`` dict.
    """

    __slots__ = ("_index", "_items")

    def __init__(self) -> None:
        self._items: list[str] = []
        self._index: dict[str, int] = {}

    def add(self, key: str) -> None:
        if key not in self._index:
            self._index[key] = len(self._items)
            self._items.append(key)

    def discard(self, key: str) -> None:
        idx = self._index.pop(key, None)
        if idx is None:
            return
        last = self._items.pop()
        if idx < len(self._items):
            self._items[idx] = last
            self._index[last] = idx

    def sample(self, k: int, rng: random.Random) -> list[str]:
        return rng.sample(self._items, min(k, len(self._items)))

    def __contains__(self, key: object) -> bool:
        return key in self._index

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)
