import pytest

from kvstore.engine.eviction import LRUPolicy, create_eviction_policy


def test_victim_is_least_recently_used() -> None:
    lru = LRUPolicy()
    for key in ("a", "b", "c"):
        lru.on_insert(key)

    lru.on_access("a")  # b is now the oldest

    assert lru.victim() == "b"


def test_remove_and_empty() -> None:
    lru = LRUPolicy()
    assert lru.victim() is None

    lru.on_insert("a")
    lru.on_insert("b")
    lru.on_remove("a")
    lru.on_remove("missing")
    lru.on_access("missing")

    assert lru.victim() == "b"
    assert len(lru) == 1


def test_factory() -> None:
    assert isinstance(create_eviction_policy("lru"), LRUPolicy)
    with pytest.raises(ValueError, match="unknown eviction policy"):
        create_eviction_policy("nope")
