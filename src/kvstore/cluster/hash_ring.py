"""Consistent hashing with virtual nodes."""

from __future__ import annotations

import bisect
import hashlib
from collections.abc import Iterable


def hash_slot_key(key: str) -> str:
    """The part of a key that is hashed. ``{user:1}:cart`` hashes as ``user:1``.

    Redis Cluster's hash tags: keys sharing a tag always land in the same
    shard group, so multi-key commands on them are allowed.
    """
    start = key.find("{")
    if start >= 0:
        end = key.find("}", start + 1)
        if end > start + 1:
            return key[start + 1 : end]
    return key


class ConsistentHashRing:
    """Maps keys to nodes so that adding/removing a node moves only ~1/N of keys.

    Each node is hashed onto the ring at ``virtual_nodes`` points; a key
    belongs to the first point clockwise from its own hash. More virtual
    nodes smooth out the key distribution at the cost of a bigger ring
    (lookup is O(log(N * virtual_nodes)) via binary search).
    """

    def __init__(self, nodes: Iterable[str] = (), *, virtual_nodes: int = 100) -> None:
        if virtual_nodes <= 0:
            raise ValueError("virtual_nodes must be positive")
        self.virtual_nodes = virtual_nodes
        self._owners: dict[int, str] = {}
        self._points: list[int] = []
        self._nodes: set[str] = set()
        for node in nodes:
            self.add_node(node)

    @staticmethod
    def _hash(value: str) -> int:
        # MD5 for its uniform spread, not security; 64 bits is plenty for a ring.
        digest = hashlib.md5(value.encode(), usedforsecurity=False).digest()
        return int.from_bytes(digest[:8], "big")

    @property
    def nodes(self) -> list[str]:
        return sorted(self._nodes)

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, node: object) -> bool:
        return node in self._nodes

    def add_node(self, node: str) -> None:
        if node in self._nodes:
            raise ValueError(f"node {node!r} is already on the ring")
        self._nodes.add(node)
        for i in range(self.virtual_nodes):
            point = self._hash(f"{node}#{i}")
            if point in self._owners:  # 64-bit collision: astronomically rare, skip it
                continue
            self._owners[point] = node
            bisect.insort(self._points, point)

    def remove_node(self, node: str) -> None:
        if node not in self._nodes:
            raise KeyError(node)
        self._nodes.remove(node)
        self._points = [p for p in self._points if self._owners[p] != node]
        self._owners = {p: n for p, n in self._owners.items() if n != node}

    def get_node(self, key: str) -> str:
        if not self._points:
            raise LookupError("the hash ring has no nodes")
        idx = bisect.bisect(self._points, self._hash(key))
        if idx == len(self._points):  # wrap around the ring
            idx = 0
        return self._owners[self._points[idx]]
