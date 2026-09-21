import random

import pytest

from kvstore.engine.eviction import (
    LFUPolicy,
    LRUPolicy,
    NoEvictionPolicy,
    RandomPolicy,
    create_eviction_policy,
)
from kvstore.engine.eviction.lfu import INIT_COUNTER, MAX_COUNTER
from tests.helpers import FakeClock


def test_lru_victim_is_least_recently_used() -> None:
    lru = LRUPolicy()
    for key in ("a", "b", "c"):
        lru.on_insert(key)
    lru.on_access("a")
    assert lru.victim() == "b"
    assert lru.victim(protect={"b"}) == "c"
    lru.on_remove("b")
    lru.on_access("missing")
    lru.on_remove("missing")
    assert len(lru) == 2
    lru.clear()
    assert lru.victim() is None


def test_lfu_frequently_used_keys_survive(clock: FakeClock) -> None:
    lfu = LFUPolicy(clock, random.Random(0), samples=50)
    for i in range(20):
        lfu.on_insert(f"k{i}")
    for _ in range(200):
        lfu.on_access("hot")  # not inserted: ignored
        for i in range(10):
            lfu.on_access(f"k{i}")  # k0..k9 are hot
    victims = set()
    for _ in range(10):
        victim = lfu.victim()
        assert victim is not None
        victims.add(victim)
        lfu.on_remove(victim)
    assert victims == {f"k{i}" for i in range(10, 20)}


def test_lfu_counter_is_logarithmic_and_saturates(clock: FakeClock) -> None:
    lfu = LFUPolicy(clock, random.Random(0))
    lfu.on_insert("k")
    assert lfu.counter("k") == INIT_COUNTER
    for _ in range(100):
        lfu.on_access("k")
    after_100 = lfu.counter("k")
    assert INIT_COUNTER < after_100 < 40  # far below 100: each increment gets less likely
    lfu._meta["k"] = (MAX_COUNTER, lfu._meta["k"][1])
    lfu.on_access("k")
    assert lfu.counter("k") == MAX_COUNTER


def test_lfu_counters_decay_with_idle_time(clock: FakeClock) -> None:
    lfu = LFUPolicy(clock, random.Random(0), samples=10)
    lfu.on_insert("was-hot")
    lfu._meta["was-hot"] = (100, lfu._meta["was-hot"][1])
    lfu.on_insert("early")
    assert lfu.victim() == "early"
    clock.advance(97 * 60)  # 97 idle minutes: 100 -> 3, early: 5 -> 0
    assert lfu.counter("was-hot") == 3
    lfu.on_remove("early")
    lfu.on_insert("new")  # a fresh key starts at 5 -- above the decayed hot key
    assert lfu.victim() == "was-hot"
    assert lfu.victim(protect={"was-hot", "new"}) is None
    assert len(lfu) == 2
    lfu.clear()
    assert len(lfu) == 0


def test_random_policy(clock: FakeClock) -> None:
    policy = RandomPolicy(random.Random(0))
    assert policy.victim() is None
    for key in "abc":
        policy.on_insert(key)
    policy.on_access("a")
    assert policy.victim() in {"a", "b", "c"}
    assert policy.victim(protect={"a", "b"}) == "c"
    assert policy.victim(protect={"a", "b", "c"}) is None
    policy.on_remove("c")
    assert len(policy) == 2
    policy.clear()
    assert len(policy) == 0


def test_noeviction_never_picks_a_victim() -> None:
    policy = NoEvictionPolicy()
    policy.on_insert("a")
    policy.on_access("a")
    assert policy.victim() is None
    assert policy.evicts is False
    assert len(policy) == 1
    policy.on_remove("a")
    policy.clear()
    assert len(policy) == 0


def test_factory(clock: FakeClock) -> None:
    rng = random.Random(0)
    for name, cls in [
        ("lru", LRUPolicy),
        ("lfu", LFUPolicy),
        ("random", RandomPolicy),
        ("noeviction", NoEvictionPolicy),
    ]:
        assert isinstance(create_eviction_policy(name, clock=clock, rng=rng), cls)
    with pytest.raises(ValueError, match="unknown eviction policy"):
        create_eviction_policy("nope", clock=clock, rng=rng)
