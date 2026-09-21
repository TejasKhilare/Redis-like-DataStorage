"""Sorted set: a dict for O(1) member -> score, plus a skip list for order (like Redis)."""

from __future__ import annotations

import math
from collections.abc import Iterator

from kvstore.core.exceptions import InvalidArgumentError
from kvstore.engine.datatypes.sizing import EMPTY_CONTAINER, ZSET_ITEM_OVERHEAD, str_size
from kvstore.engine.datatypes.skiplist import Node, SkipList


class SortedSet:
    __slots__ = ("_list", "_scores", "nbytes")

    type_name = "zset"

    def __init__(self) -> None:
        self._scores: dict[str, float] = {}
        self._list = SkipList()
        self.nbytes = EMPTY_CONTAINER

    def __len__(self) -> int:
        return len(self._scores)

    def __contains__(self, member: object) -> bool:
        return member in self._scores

    def score(self, member: str) -> float | None:
        return self._scores.get(member)

    def add(self, member: str, score: float) -> bool:
        """Insert or re-score a member. Returns True if the member is new."""
        old = self._scores.get(member)
        if old is None:
            self._scores[member] = score
            self._list.insert(score, member)
            self.nbytes += str_size(member) + ZSET_ITEM_OVERHEAD
            return True
        if old != score:
            self._list.delete(old, member)
            self._list.insert(score, member)
            self._scores[member] = score
        return False

    def incr(self, member: str, delta: float) -> float:
        new = self._scores.get(member, 0.0) + delta
        if math.isnan(new):
            raise InvalidArgumentError("resulting score is not a number (NaN)")
        self.add(member, new)
        return new

    def remove(self, member: str) -> bool:
        score = self._scores.pop(member, None)
        if score is None:
            return False
        self._list.delete(score, member)
        self.nbytes -= str_size(member) + ZSET_ITEM_OVERHEAD
        return True

    def rank(self, member: str, *, reverse: bool = False) -> int | None:
        """0-based rank, or None if the member is absent."""
        score = self._scores.get(member)
        if score is None:
            return None
        rank = self._list.rank(score, member) - 1
        return len(self) - 1 - rank if reverse else rank

    def range_by_rank(self, start: int, stop: int, *, reverse: bool = False) -> list[Node]:
        """Elements with 0-based rank in [start, stop]; negative indexes count from the end."""
        length = len(self)
        if start < 0:
            start = max(0, length + start)
        if stop < 0:
            stop += length
        stop = min(stop, length - 1)
        if start > stop or start >= length:
            return []
        count = stop - start + 1
        if reverse:
            node = self._list.by_rank(length - start)
            return self._walk(node, count, backward=True)
        return self._walk(self._list.by_rank(start + 1), count, backward=False)

    def range_by_score(
        self,
        lo: float,
        hi: float,
        *,
        lo_ex: bool = False,
        hi_ex: bool = False,
        reverse: bool = False,
        offset: int = 0,
        count: int = -1,
    ) -> list[Node]:
        if reverse:
            node = self._list.last_in_range(lo, hi, lo_ex, hi_ex)
        else:
            node = self._list.first_in_range(lo, hi, lo_ex, hi_ex)
        result: list[Node] = []
        while node is not None and offset > 0:
            node = node.backward if reverse else node.forward[0]
            offset -= 1
        while node is not None and count != 0:
            in_range = (node.score > lo if lo_ex else node.score >= lo) and (
                node.score < hi if hi_ex else node.score <= hi
            )
            if not in_range:
                break
            result.append(node)
            count -= 1
            node = node.backward if reverse else node.forward[0]
        return result

    def count_in_range(self, lo: float, hi: float, lo_ex: bool, hi_ex: bool) -> int:
        """O(log n): difference of the ranks of the first and last element in range."""
        first = self._list.first_in_range(lo, hi, lo_ex, hi_ex)
        if first is None:
            return 0
        last = self._list.last_in_range(lo, hi, lo_ex, hi_ex)
        assert last is not None
        return (
            self._list.rank(last.score, last.member)
            - self._list.rank(first.score, first.member)
            + 1
        )

    def pop(self, count: int, *, from_max: bool = False) -> list[tuple[str, float]]:
        popped: list[tuple[str, float]] = []
        while count > 0 and self._scores:
            node = self._list.tail if from_max else self._list.header.forward[0]
            assert node is not None
            popped.append((node.member, node.score))
            self.remove(node.member)
            count -= 1
        return popped

    def items(self) -> Iterator[tuple[str, float]]:
        """All (member, score) pairs in ascending order."""
        for node in self._list:
            yield node.member, node.score

    @staticmethod
    def _walk(node: Node | None, count: int, *, backward: bool) -> list[Node]:
        result: list[Node] = []
        while node is not None and len(result) < count:
            result.append(node)
            node = node.backward if backward else node.forward[0]
        return result
