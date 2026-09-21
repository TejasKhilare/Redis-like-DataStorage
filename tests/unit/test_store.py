import random

import pytest

from kvstore.engine.eviction import LRUPolicy
from kvstore.engine.store import Store
from tests.helpers import FakeClock


def make_store(clock: FakeClock, max_keys: int = 100) -> Store:
    return Store(max_keys, LRUPolicy(), clock=clock, rng=random.Random(0))


def test_rejects_non_positive_capacity(clock: FakeClock) -> None:
    with pytest.raises(ValueError, match="max_keys"):
        make_store(clock, max_keys=0)


def test_set_get_delete(clock: FakeClock) -> None:
    store = make_store(clock)
    store.set("a", 1)

    entry = store.get("a")
    assert entry is not None
    assert entry.value == 1
    assert store.delete("a") is True
    assert store.delete("a") is False
    assert store.get("a") is None


def test_eviction_removes_the_data_too(clock: FakeClock) -> None:
    """Regression: evicted keys used to stay readable because only the LRU list was trimmed."""
    store = make_store(clock, max_keys=3)
    for key in ("k1", "k2", "k3"):
        store.set(key, key)
    store.get("k1")  # k2 becomes least recently used

    evicted = store.set("k4", "k4")

    assert evicted == ["k2"]
    assert store.get("k2") is None
    assert len(store) == 3
    assert store.evicted_keys == 1


def test_overwrite_does_not_evict_and_clears_ttl(clock: FakeClock) -> None:
    store = make_store(clock, max_keys=2)
    store.set("a", 1)
    store.set("b", 2)
    store.set_expiry("a", clock.now + 10)

    assert store.set("a", 3) == []
    entry = store.peek("a")
    assert entry is not None
    assert entry.expires_at is None
    assert store.volatile_count == 0


def test_eviction_can_be_disabled(clock: FakeClock) -> None:
    store = make_store(clock, max_keys=1)
    store.eviction_enabled = False
    store.set("a", 1)
    store.set("b", 2)
    assert len(store) == 2

    store.eviction_enabled = True
    assert store.evict_if_needed() == ["a"]


def test_lazy_expiry(clock: FakeClock) -> None:
    store = make_store(clock)
    store.set("a", 1)
    assert store.set_expiry("a", clock.now + 5)

    clock.advance(4.999)
    assert store.get("a") is not None
    clock.advance(0.001)
    assert store.get("a") is None
    assert store.expired_keys == 1
    assert store.volatile_count == 0


def test_expiry_on_missing_key(clock: FakeClock) -> None:
    store = make_store(clock)
    assert store.set_expiry("nope", clock.now + 5) is False
    assert store.persist("nope") is False


def test_persist(clock: FakeClock) -> None:
    store = make_store(clock)
    store.set("a", 1)
    assert store.persist("a") is False  # no TTL to remove
    store.set_expiry("a", clock.now + 5)
    assert store.persist("a") is True

    clock.advance(10)
    assert store.get("a") is not None


def test_active_expiry_reclaims_keys_nobody_reads(clock: FakeClock) -> None:
    store = make_store(clock, max_keys=10_000)
    for i in range(1000):
        store.set(f"tmp{i}", i)
        store.set_expiry(f"tmp{i}", clock.now + 1)
    for i in range(100):
        store.set(f"keep{i}", i)

    clock.advance(2)
    removed = sum(store.expire_cycle(sample_size=20) for _ in range(100))

    assert removed == 1000
    assert len(store) == 100
    assert store.volatile_count == 0


def test_active_expiry_stops_early_when_few_keys_are_stale(clock: FakeClock) -> None:
    store = make_store(clock)
    for i in range(50):
        store.set(f"k{i}", i)
        store.set_expiry(f"k{i}", clock.now + 100)

    assert store.expire_cycle(sample_size=20) == 0
    assert store.volatile_count == 50
