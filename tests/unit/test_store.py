import random

import pytest

from kvstore.core.exceptions import WrongTypeError
from kvstore.engine.datatypes import HashValue, ListValue, SetValue, SortedSet
from kvstore.engine.eviction import EvictionPolicy, LRUPolicy
from kvstore.engine.store import Store
from tests.helpers import FakeClock


def make_store(
    clock: FakeClock, *, policy: EvictionPolicy | None = None, max_keys: int = 0, maxmemory: int = 0
) -> Store:
    return Store(
        policy or LRUPolicy(),
        max_keys=max_keys,
        maxmemory=maxmemory,
        clock=clock,
        rng=random.Random(0),
    )


def test_rejects_negative_limits(clock: FakeClock) -> None:
    with pytest.raises(ValueError, match="limits"):
        make_store(clock, max_keys=-1)


def test_set_get_delete_and_stats(clock: FakeClock) -> None:
    store = make_store(clock)
    store.set("a", "1")
    assert store.read_str("a") == "1"
    assert store.read_str("missing") is None
    assert (store.keyspace_hits, store.keyspace_misses) == (1, 1)
    assert store.delete("a") is True
    assert store.delete("a") is False


def test_type_checks(clock: FakeClock) -> None:
    store = make_store(clock)
    store.set("s", "x")
    store.write_target("l", ListValue).push_right(["a"])
    with pytest.raises(WrongTypeError):
        store.read_str("l")
    with pytest.raises(WrongTypeError):
        store.read("s", ListValue)
    with pytest.raises(WrongTypeError):
        store.write_target("s", HashValue)
    assert store.read("missing", SetValue) is None


def test_refresh_drops_empty_collections(clock: FakeClock) -> None:
    store = make_store(clock)
    target = store.write_target("l", ListValue)
    target.push_right(["a"])
    store.refresh("l")
    target.pop(1, left=True)
    store.refresh("l")
    assert store.peek("l") is None
    assert store.used_memory == 0
    store.refresh("never-existed")  # no-op


def test_memory_accounting_returns_to_zero(clock: FakeClock) -> None:
    store = make_store(clock)
    store.set("s", "value")
    for kind, fill in [
        (ListValue, lambda v: v.push_right(["a", "b"])),
        (HashValue, lambda v: v.set("f", "v")),
        (SetValue, lambda v: v.add("m")),
        (SortedSet, lambda v: v.add("m", 1.0)),
    ]:
        key = kind.__name__
        fill(store.write_target(key, kind))  # type: ignore[arg-type]
        store.refresh(key)
    before = store.used_memory
    assert before > 0
    store.set("s", "a much longer value than before")
    assert store.used_memory > before
    for key in ["s", "ListValue", "HashValue", "SetValue", "SortedSet"]:
        store.delete(key)
    assert store.used_memory == 0
    assert len(store) == 0


def test_max_keys_eviction_removes_the_data_too(clock: FakeClock) -> None:
    """Regression (phase 1): evicted keys used to stay readable."""
    store = make_store(clock, max_keys=3)
    for key in ("k1", "k2", "k3"):
        store.set(key, key)
    store.read_str("k1")  # k2 becomes least recently used
    store.set("k4", "k4")
    assert store.evict_if_needed(protect={"k4"}) == ["k2"]
    assert store.read_str("k2") is None
    assert len(store) == 3


def test_eviction_protects_the_commands_keys(clock: FakeClock) -> None:
    store = make_store(clock, max_keys=2)
    store.set("old", "x")
    store.set("new", "x")
    store.set("newest", "x")
    assert store.evict_if_needed(protect={"old"}) == ["new"]
    assert store.evicted_keys == 1


def test_maxmemory_eviction(clock: FakeClock) -> None:
    store = make_store(clock, maxmemory=2000)
    for i in range(50):
        store.set(f"k{i}", "x" * 50)
        store.evict_if_needed(protect={f"k{i}"})
    assert store.used_memory <= 2000
    assert store.peek("k49") is not None
    assert store.peek("k0") is None


def test_over_limit_counts_pending_keys(clock: FakeClock) -> None:
    store = make_store(clock, max_keys=2)
    store.set("a", "1")
    assert not store.over_limit(extra_keys=1)
    assert store.over_limit(extra_keys=2)


def test_eviction_can_be_disabled(clock: FakeClock) -> None:
    store = make_store(clock, max_keys=1)
    store.eviction_enabled = False
    store.set("a", "1")
    store.set("b", "2")
    assert store.evict_if_needed() == []
    store.eviction_enabled = True
    assert store.evict_if_needed() == ["a"]


def test_keep_ttl(clock: FakeClock) -> None:
    store = make_store(clock)
    store.set("a", "1")
    store.set_expiry("a", clock.now + 10)
    store.set("a", "2", keep_ttl=True)
    entry = store.peek("a")
    assert entry is not None
    assert entry.expires_at == clock.now + 10
    assert store.volatile_count == 1
    store.set("a", "3")
    assert store.volatile_count == 0


def test_lazy_expiry(clock: FakeClock) -> None:
    store = make_store(clock)
    store.set("a", "1")
    assert store.set_expiry("a", clock.now + 5)
    clock.advance(4.999)
    assert store.read_str("a") == "1"
    clock.advance(0.001)
    assert store.read_str("a") is None
    assert store.expired_keys == 1
    assert store.set_expiry("a", clock.now + 5) is False
    assert store.persist("a") is False


def test_expiry_can_be_disabled_while_loading(clock: FakeClock) -> None:
    store = make_store(clock)
    store.set("a", "1")
    store.set_expiry("a", clock.now - 1)
    store.expiry_enabled = False
    assert store.peek("a") is not None
    store.expiry_enabled = True
    assert store.purge_expired() == 1
    assert len(store) == 0


def test_active_expiry(clock: FakeClock) -> None:
    store = make_store(clock)
    for i in range(1000):
        store.set(f"tmp{i}", "x")
        store.set_expiry(f"tmp{i}", clock.now + 1)
    for i in range(100):
        store.set(f"keep{i}", "x")
    clock.advance(2)
    removed = sum(store.expire_cycle(sample_size=20) for _ in range(100))
    assert removed == 1000
    assert len(store) == 100


def test_iter_keys_skips_expired(clock: FakeClock) -> None:
    store = make_store(clock)
    store.set("live", "x")
    store.set("dead", "x")
    store.set_expiry("dead", clock.now + 1)
    clock.advance(2)
    assert list(store.iter_keys()) == ["live"]


def test_snapshot_is_a_detached_copy(clock: FakeClock) -> None:
    store = make_store(clock)
    store.set("s", "v")
    store.set_expiry("s", clock.now + 100)
    store.write_target("l", ListValue).push_right(["a", "b"])
    store.write_target("h", HashValue).set("f", "v")
    store.write_target("st", SetValue).add("m")
    store.write_target("z", SortedSet).add("m", 2.5)
    store.set("gone", "x")
    store.set_expiry("gone", clock.now + 1)
    clock.advance(2)

    records = store.snapshot()
    store.read("l", ListValue).push_right(["mutated"])  # type: ignore[union-attr]

    by_key = {record[0]: record for record in records}
    assert "gone" not in by_key
    assert by_key["l"] == ("l", "list", ("a", "b"), None)  # tuples: invisible to the GC
    assert by_key["s"][3] == clock.now + 98

    copy = make_store(clock)
    for record in records:
        copy.load_record(record)
    assert copy.read("z", SortedSet).score("m") == 2.5  # type: ignore[union-attr]
    assert copy.read("h", HashValue).get("f") == "v"  # type: ignore[union-attr]
    assert copy.volatile_count == 1
    with pytest.raises(ValueError, match="unknown value type"):
        copy.load_record(("k", "stream", [], None))


def test_clear(clock: FakeClock) -> None:
    store = make_store(clock)
    store.set("a", "1")
    store.set_expiry("a", clock.now + 5)
    store.clear()
    assert (len(store), store.used_memory, store.volatile_count) == (0, 0, 0)
