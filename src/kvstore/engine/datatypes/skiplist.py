"""Skip list ordered by (score, member), with rank spans -- a port of Redis's ``zskiplist``.

Every forward pointer stores its *span*: how many level-0 nodes it jumps
over. Summing spans along the search path gives a node's rank, so rank
queries and index lookups are O(log n) on average, as are insert and delete.

Why a skip list and not a balanced tree? Same asymptotics, far simpler code,
range scans are a plain walk along level 0, and (as Redis's author notes)
tuning memory vs. speed is just the promotion probability ``P``.
"""

from __future__ import annotations

import random
from collections.abc import Iterator

MAX_LEVEL = 32
P = 0.25  # Redis's choice: ~1.33 pointers per node on average

_rng = random.Random()


class Node:
    __slots__ = ("backward", "forward", "member", "score", "span")

    def __init__(self, level: int, member: str, score: float) -> None:
        self.member = member
        self.score = score
        self.backward: Node | None = None
        self.forward: list[Node | None] = [None] * level
        self.span: list[int] = [0] * level


def _before(node: Node, score: float, member: str) -> bool:
    """Is ``node`` ordered strictly before (score, member)?"""
    return node.score < score or (node.score == score and node.member < member)


class SkipList:
    __slots__ = ("header", "length", "level", "tail")

    def __init__(self) -> None:
        self.header = Node(MAX_LEVEL, "", 0.0)
        self.tail: Node | None = None
        self.level = 1
        self.length = 0

    def __len__(self) -> int:
        return self.length

    @staticmethod
    def _random_level() -> int:
        level = 1
        while level < MAX_LEVEL and _rng.random() < P:
            level += 1
        return level

    def insert(self, score: float, member: str) -> Node:
        """Insert a member that is not already present."""
        update: list[Node] = [self.header] * MAX_LEVEL
        rank = [0] * MAX_LEVEL
        x = self.header
        for i in range(self.level - 1, -1, -1):
            rank[i] = 0 if i == self.level - 1 else rank[i + 1]
            nxt = x.forward[i]
            while nxt is not None and _before(nxt, score, member):
                rank[i] += x.span[i]
                x = nxt
                nxt = x.forward[i]
            update[i] = x

        level = self._random_level()
        if level > self.level:
            for i in range(self.level, level):
                rank[i] = 0
                update[i] = self.header
                self.header.span[i] = self.length
            self.level = level

        node = Node(level, member, score)
        for i in range(level):
            prev = update[i]
            node.forward[i] = prev.forward[i]
            prev.forward[i] = node
            node.span[i] = prev.span[i] - (rank[0] - rank[i])
            prev.span[i] = (rank[0] - rank[i]) + 1
        for i in range(level, self.level):
            update[i].span[i] += 1

        node.backward = None if update[0] is self.header else update[0]
        after = node.forward[0]
        if after is not None:
            after.backward = node
        else:
            self.tail = node
        self.length += 1
        return node

    def delete(self, score: float, member: str) -> bool:
        update: list[Node] = [self.header] * MAX_LEVEL
        x = self.header
        for i in range(self.level - 1, -1, -1):
            nxt = x.forward[i]
            while nxt is not None and _before(nxt, score, member):
                x = nxt
                nxt = x.forward[i]
            update[i] = x
        target = x.forward[0]
        if target is None or target.score != score or target.member != member:
            return False
        self._unlink(target, update)
        return True

    def _unlink(self, node: Node, update: list[Node]) -> None:
        for i in range(self.level):
            prev = update[i]
            if prev.forward[i] is node:
                prev.span[i] += node.span[i] - 1
                prev.forward[i] = node.forward[i]
            else:
                prev.span[i] -= 1
        after = node.forward[0]
        if after is not None:
            after.backward = node.backward
        else:
            self.tail = node.backward
        while self.level > 1 and self.header.forward[self.level - 1] is None:
            self.level -= 1
        self.length -= 1

    def rank(self, score: float, member: str) -> int:
        """1-based rank of the element, or 0 if it is not in the list."""
        traversed = 0
        x = self.header
        for i in range(self.level - 1, -1, -1):
            nxt = x.forward[i]
            while nxt is not None and (
                nxt.score < score or (nxt.score == score and nxt.member <= member)
            ):
                traversed += x.span[i]
                x = nxt
                nxt = x.forward[i]
            if x is not self.header and x.member == member:
                return traversed
        return 0

    def by_rank(self, rank: int) -> Node | None:
        """The element at 1-based ``rank``."""
        traversed = 0
        x = self.header
        for i in range(self.level - 1, -1, -1):
            nxt = x.forward[i]
            while nxt is not None and traversed + x.span[i] <= rank:
                traversed += x.span[i]
                x = nxt
                nxt = x.forward[i]
            if traversed == rank:
                return x
        return None

    def first_in_range(self, lo: float, hi: float, lo_ex: bool, hi_ex: bool) -> Node | None:
        """First element with a score inside the range."""
        if not self._range_may_overlap(lo, hi, lo_ex, hi_ex):
            return None
        x = self.header
        for i in range(self.level - 1, -1, -1):
            nxt = x.forward[i]
            while nxt is not None and not _gte_min(nxt.score, lo, lo_ex):
                x = nxt
                nxt = x.forward[i]
        candidate = x.forward[0]
        if candidate is None or not _lte_max(candidate.score, hi, hi_ex):
            return None
        return candidate

    def last_in_range(self, lo: float, hi: float, lo_ex: bool, hi_ex: bool) -> Node | None:
        """Last element with a score inside the range."""
        if not self._range_may_overlap(lo, hi, lo_ex, hi_ex):
            return None
        x = self.header
        for i in range(self.level - 1, -1, -1):
            nxt = x.forward[i]
            while nxt is not None and _lte_max(nxt.score, hi, hi_ex):
                x = nxt
                nxt = x.forward[i]
        if x is self.header or not _gte_min(x.score, lo, lo_ex):
            return None
        return x

    def _range_may_overlap(self, lo: float, hi: float, lo_ex: bool, hi_ex: bool) -> bool:
        if lo > hi or (lo == hi and (lo_ex or hi_ex)):
            return False
        first, last = self.header.forward[0], self.tail
        if first is None or last is None:
            return False
        return _gte_min(last.score, lo, lo_ex) and _lte_max(first.score, hi, hi_ex)

    def __iter__(self) -> Iterator[Node]:
        node = self.header.forward[0]
        while node is not None:
            yield node
            node = node.forward[0]


def _gte_min(score: float, lo: float, exclusive: bool) -> bool:
    return score > lo if exclusive else score >= lo


def _lte_max(score: float, hi: float, exclusive: bool) -> bool:
    return score < hi if exclusive else score <= hi
