"""Approximate memory accounting.

Python can't cheaply report the deep size of a container, so every data
type keeps a running byte estimate that it updates on each mutation. The
store sums them into ``used_memory``, which drives ``maxmemory`` eviction.
The constants approximate CPython's per-element costs on 64-bit builds;
the goal is "proportional and cheap", not byte-exact.
"""

from __future__ import annotations

import sys

# Per-key cost: Entry object (__slots__) + keyspace dict slot.
ENTRY_OVERHEAD = 120

# Per-element container costs on top of the element strings themselves.
LIST_ITEM_OVERHEAD = 8  # a pointer in a deque block
HASH_ITEM_OVERHEAD = 50  # dict entry + index slot, with growth slack
SET_ITEM_OVERHEAD = 60  # dict entry + slot in the sampling array
ZSET_ITEM_OVERHEAD = 200  # dict entry + skip list node (~1.33 levels)

EMPTY_CONTAINER = 200


def str_size(value: str) -> int:
    return sys.getsizeof(value)
