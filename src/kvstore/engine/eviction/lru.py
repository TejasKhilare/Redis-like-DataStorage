"""Least-recently-used eviction in O(1) per operation."""

from __future__ import annotations

from collections import OrderedDict

from kvstore.engine.eviction.base import EvictionPolicy


class LRUPolicy(EvictionPolicy):
    """Exact LRU backed by an ``OrderedDict`` (a hash map + doubly linked list).

    Oldest key at the front, most recently used at the back. Redis uses
    *approximate* LRU (sampling 5 keys) to save the 16 bytes of list pointers
    per key; exact LRU is simpler and fine at this scale.
    """

    name = "lru"

    def __init__(self) -> None:
        self._order: OrderedDict[str, None] = OrderedDict()

    def on_insert(self, key: str) -> None:
        self._order[key] = None
        self._order.move_to_end(key)

    def on_access(self, key: str) -> None:
        if key in self._order:
            self._order.move_to_end(key)

    def on_remove(self, key: str) -> None:
        self._order.pop(key, None)

    def victim(self) -> str | None:
        return next(iter(self._order), None)

    def __len__(self) -> int:
        return len(self._order)
