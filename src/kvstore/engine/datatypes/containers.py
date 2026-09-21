"""List, hash and set values.

Thin wrappers over Python containers whose one job is to keep ``nbytes``
(the memory estimate) correct on every mutation -- so the store can account
memory in O(1) per command instead of re-measuring a whole container.
"""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Iterable, Iterator
from itertools import islice

from kvstore.engine.datatypes.sizing import (
    EMPTY_CONTAINER,
    HASH_ITEM_OVERHEAD,
    LIST_ITEM_OVERHEAD,
    SET_ITEM_OVERHEAD,
    str_size,
)
from kvstore.engine.keyset import SampleableKeySet


class ListValue:
    """Double-ended list: O(1) push/pop at both ends (Redis uses a quicklist)."""

    __slots__ = ("_items", "nbytes")

    type_name = "list"

    def __init__(self, items: Iterable[str] = ()) -> None:
        self._items: deque[str] = deque()
        self.nbytes = EMPTY_CONTAINER
        self.push_right(items)

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def push_left(self, values: Iterable[str]) -> None:
        for value in values:
            self._items.appendleft(value)
            self.nbytes += str_size(value) + LIST_ITEM_OVERHEAD

    def push_right(self, values: Iterable[str]) -> None:
        for value in values:
            self._items.append(value)
            self.nbytes += str_size(value) + LIST_ITEM_OVERHEAD

    def pop(self, count: int, *, left: bool) -> list[str]:
        popped = []
        for _ in range(min(count, len(self._items))):
            value = self._items.popleft() if left else self._items.pop()
            self.nbytes -= str_size(value) + LIST_ITEM_OVERHEAD
            popped.append(value)
        return popped

    def normalize(self, index: int) -> int | None:
        """Turn a possibly negative index into a valid position, or None."""
        if index < 0:
            index += len(self._items)
        return index if 0 <= index < len(self._items) else None

    def get(self, index: int) -> str | None:
        pos = self.normalize(index)
        return None if pos is None else self._items[pos]

    def set(self, index: int, value: str) -> bool:
        pos = self.normalize(index)
        if pos is None:
            return False
        self.nbytes += str_size(value) - str_size(self._items[pos])
        self._items[pos] = value
        return True

    def range(self, start: int, stop: int) -> list[str]:
        bounds = _clamp(start, stop, len(self._items))
        if bounds is None:
            return []
        lo, hi = bounds
        length = len(self._items)
        # deques can't be sliced; walk in from whichever end is closer.
        if lo > length - 1 - hi:
            tail = list(islice(reversed(self._items), length - 1 - hi, length - lo))
            tail.reverse()
            return tail
        return list(islice(self._items, lo, hi + 1))

    def trim(self, start: int, stop: int) -> None:
        keep = self.range(start, stop)
        self._items = deque()
        self.nbytes = EMPTY_CONTAINER
        self.push_right(keep)

    def remove(self, count: int, value: str) -> int:
        """LREM: count > 0 from head, < 0 from tail, 0 = all occurrences."""
        items = list(self._items)
        if count < 0:
            items.reverse()
        kept, removed = [], 0
        for item in items:
            if item == value and (count == 0 or removed < abs(count)):
                removed += 1
                continue
            kept.append(item)
        if removed:
            if count < 0:
                kept.reverse()
            self._items = deque(kept)
            self.nbytes -= removed * (str_size(value) + LIST_ITEM_OVERHEAD)
        return removed


class HashValue:
    __slots__ = ("_fields", "nbytes")

    type_name = "hash"

    def __init__(self) -> None:
        self._fields: dict[str, str] = {}
        self.nbytes = EMPTY_CONTAINER

    def __len__(self) -> int:
        return len(self._fields)

    def __contains__(self, field: object) -> bool:
        return field in self._fields

    def get(self, field: str) -> str | None:
        return self._fields.get(field)

    def set(self, field: str, value: str) -> bool:
        """Returns True if the field is new."""
        old = self._fields.get(field)
        self._fields[field] = value
        if old is None:
            self.nbytes += str_size(field) + str_size(value) + HASH_ITEM_OVERHEAD
            return True
        self.nbytes += str_size(value) - str_size(old)
        return False

    def delete(self, field: str) -> bool:
        old = self._fields.pop(field, None)
        if old is None:
            return False
        self.nbytes -= str_size(field) + str_size(old) + HASH_ITEM_OVERHEAD
        return True

    def items(self) -> Iterator[tuple[str, str]]:
        return iter(self._fields.items())


class SetValue:
    """Unordered set with O(1) random member selection (for SPOP / SRANDMEMBER)."""

    __slots__ = ("_members", "nbytes")

    type_name = "set"

    def __init__(self) -> None:
        self._members = SampleableKeySet()
        self.nbytes = EMPTY_CONTAINER

    def __len__(self) -> int:
        return len(self._members)

    def __contains__(self, member: object) -> bool:
        return member in self._members

    def __iter__(self) -> Iterator[str]:
        return iter(self._members)

    def add(self, member: str) -> bool:
        if member in self._members:
            return False
        self._members.add(member)
        self.nbytes += str_size(member) + SET_ITEM_OVERHEAD
        return True

    def remove(self, member: str) -> bool:
        if member not in self._members:
            return False
        self._members.discard(member)
        self.nbytes -= str_size(member) + SET_ITEM_OVERHEAD
        return True

    def sample(self, count: int, rng: random.Random) -> list[str]:
        return self._members.sample(count, rng)


def _clamp(start: int, stop: int, length: int) -> tuple[int, int] | None:
    """Redis range semantics: inclusive, negative from the end, clamped."""
    if start < 0:
        start = max(0, length + start)
    if stop < 0:
        stop += length
    stop = min(stop, length - 1)
    if start > stop or start >= length:
        return None
    return start, stop
