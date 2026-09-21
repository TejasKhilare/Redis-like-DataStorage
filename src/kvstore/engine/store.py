"""In-memory keyspace: typed values, lazy + active expiry, limits and eviction."""

from __future__ import annotations

import random
import time
from collections.abc import Container, Iterator
from typing import Any, TypeVar

from kvstore.core.codec import SnapshotRecord
from kvstore.core.exceptions import WrongTypeError
from kvstore.engine.datatypes import (
    HashValue,
    ListValue,
    SetValue,
    SortedSet,
    Value,
    type_name,
    value_size,
)
from kvstore.engine.datatypes.sizing import ENTRY_OVERHEAD, str_size
from kvstore.engine.entry import Clock, Entry
from kvstore.engine.eviction import EvictionPolicy
from kvstore.engine.keyset import SampleableKeySet

# Active expiry keeps sampling while more than 1/4 of a sample was expired,
# but never more than this many rounds per cycle so one cycle can't stall
# the event loop (Redis bounds the cycle by time instead).
_MAX_EXPIRY_ROUNDS = 16

V = TypeVar("V", ListValue, HashValue, SetValue, SortedSet)


class Store:
    """The keyspace.

    Invariants:
    * every key in ``_data`` is tracked by the eviction policy;
    * ``_volatile`` holds exactly the keys whose entry has a TTL;
    * ``used_memory`` is the sum of every entry's ``size``;
    * no collection is ever stored empty (an empty list/hash/set/zset is
      removed, as in Redis).

    Every removal goes through :meth:`_remove` and every size change through
    :meth:`refresh`, which keep all of these in sync.
    """

    def __init__(
        self,
        eviction: EvictionPolicy,
        *,
        max_keys: int = 0,
        maxmemory: int = 0,
        clock: Clock = time.time,
        rng: random.Random | None = None,
    ) -> None:
        if max_keys < 0 or maxmemory < 0:
            raise ValueError("limits must be >= 0 (0 = unlimited)")
        self.max_keys = max_keys
        self.maxmemory = maxmemory
        # Both are switched off while the AOF is replayed (see Engine.open).
        self.eviction_enabled = True
        self.expiry_enabled = True
        self.used_memory = 0
        self.expired_keys = 0
        self.evicted_keys = 0
        self.keyspace_hits = 0
        self.keyspace_misses = 0
        self._eviction = eviction
        self._clock = clock
        self._rng = rng or random.Random()
        self._data: dict[str, Entry] = {}
        self._volatile = SampleableKeySet()

    def __len__(self) -> int:
        return len(self._data)

    @property
    def rng(self) -> random.Random:
        return self._rng

    @property
    def eviction_policy(self) -> EvictionPolicy:
        return self._eviction

    @property
    def volatile_count(self) -> int:
        return len(self._volatile)

    # ------------------------------------------------------------ reads
    def get(self, key: str) -> Entry | None:
        """Look a key up for a read: counts hits/misses and marks it as used."""
        entry = self._lookup(key)
        if entry is None:
            self.keyspace_misses += 1
            return None
        self.keyspace_hits += 1
        self._eviction.on_access(key)
        return entry

    def peek(self, key: str) -> Entry | None:
        """Look a key up without side effects on recency or stats (EXISTS, TTL, TYPE)."""
        return self._lookup(key)

    def read_str(self, key: str) -> str | None:
        entry = self.get(key)
        if entry is None:
            return None
        if not isinstance(entry.value, str):
            raise WrongTypeError()
        return entry.value

    def read(self, key: str, kind: type[V]) -> V | None:
        """The value at ``key`` if it has type ``kind``; None if missing; WRONGTYPE otherwise."""
        entry = self.get(key)
        if entry is None:
            return None
        if not isinstance(entry.value, kind):
            raise WrongTypeError()
        return entry.value

    def write_target(self, key: str, kind: type[V]) -> V:
        """The collection at ``key`` for modification, created empty if missing.

        Callers must validate every argument *before* calling this, and the
        engine calls :meth:`refresh` afterwards (which also drops the key if
        the command left the collection empty).
        """
        entry = self._lookup(key)
        if entry is not None:
            if not isinstance(entry.value, kind):
                raise WrongTypeError()
            self._eviction.on_access(key)
            return entry.value
        value = kind()
        self._insert(key, Entry(value))
        return value

    def iter_keys(self) -> Iterator[str]:
        """Live keys (expired ones are reclaimed on the way)."""
        for key in list(self._data):
            if self._lookup(key) is not None:
                yield key

    # ----------------------------------------------------------- writes
    def set(self, key: str, value: Value, *, keep_ttl: bool = False) -> None:
        """Store ``value`` at ``key``, replacing any value. Clears the TTL unless ``keep_ttl``."""
        old = self._lookup(key) if keep_ttl else self._data.get(key)
        if old is None:
            self._insert(key, Entry(value))
            return
        self.used_memory -= old.size
        entry = Entry(value, old.expires_at if keep_ttl else None)
        if entry.expires_at is None:
            self._volatile.discard(key)
        entry.size = self._entry_size(key, value)
        self.used_memory += entry.size
        self._data[key] = entry
        self._eviction.on_access(key)

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

    def refresh(self, key: str) -> None:
        """Re-account a key after its value was mutated in place; drop it if now empty."""
        entry = self._data.get(key)
        if entry is None:
            return
        if not isinstance(entry.value, str) and len(entry.value) == 0:
            self._remove(key)
            return
        size = self._entry_size(key, entry.value)
        self.used_memory += size - entry.size
        entry.size = size

    def clear(self) -> None:
        self._data.clear()
        self._volatile = SampleableKeySet()
        self._eviction.clear()
        self.used_memory = 0

    # ----------------------------------------------------------- limits
    def over_limit(self, extra_keys: int = 0) -> bool:
        if self.max_keys and len(self._data) + extra_keys > self.max_keys:
            return True
        return bool(self.maxmemory) and self.used_memory > self.maxmemory

    def evict_if_needed(self, protect: Container[str] = ()) -> list[str]:
        if not self.eviction_enabled or not self._eviction.evicts:
            return []
        evicted: list[str] = []
        while self.over_limit():
            victim = self._eviction.victim(protect)
            if victim is None:
                break  # only protected keys left: tolerate the overshoot
            self._remove(victim)
            self.evicted_keys += 1
            evicted.append(victim)
        return evicted

    # ------------------------------------------------------- expiration
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

    # ---------------------------------------------------------- snapshots
    def snapshot(self) -> list[SnapshotRecord]:
        """A point-in-time copy of every live key, safe to serialize on another thread.

        Strings are immutable and shared; collections are copied into plain
        Python containers. This O(n) copy is what replaces fork()'s
        copy-on-write, which Redis uses to snapshot without pausing.
        """
        now = self._clock()
        records: list[SnapshotRecord] = []
        for key, entry in self._data.items():
            if entry.is_expired(now):
                continue
            value = entry.value
            payload: Any
            if isinstance(value, str):
                payload = value
            elif isinstance(value, ListValue | SetValue):
                payload = list(value)
            elif isinstance(value, HashValue):
                payload = list(value.items())
            else:
                payload = list(value.items())
            records.append((key, type_name(value), payload, entry.expires_at))
        return records

    def load_record(self, record: SnapshotRecord) -> None:
        key, kind, payload, expires_at = record
        value: Value
        if kind == "string":
            value = payload
        elif kind == "list":
            value = ListValue(payload)
        elif kind == "hash":
            value = HashValue()
            for field, field_value in payload:
                value.set(field, field_value)
        elif kind == "set":
            value = SetValue()
            for member in payload:
                value.add(member)
        elif kind == "zset":
            value = SortedSet()
            for member, score in payload:
                value.add(member, score)
        else:
            raise ValueError(f"unknown value type {kind!r}")
        self.set(key, value)
        if expires_at is not None:
            self.set_expiry(key, expires_at)

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

    def _insert(self, key: str, entry: Entry) -> None:
        entry.size = self._entry_size(key, entry.value)
        self.used_memory += entry.size
        self._data[key] = entry
        self._eviction.on_insert(key)

    def _remove(self, key: str) -> None:
        entry = self._data.pop(key)
        self.used_memory -= entry.size
        self._volatile.discard(key)
        self._eviction.on_remove(key)

    @staticmethod
    def _entry_size(key: str, value: Value) -> int:
        return str_size(key) + ENTRY_OVERHEAD + value_size(value)
