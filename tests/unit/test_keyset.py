import random

from kvstore.engine.keyset import SampleableKeySet


def test_add_is_idempotent() -> None:
    keys = SampleableKeySet()
    keys.add("a")
    keys.add("a")
    assert len(keys) == 1
    assert "a" in keys


def test_discard_keeps_array_dense() -> None:
    keys = SampleableKeySet()
    for key in "abcde":
        keys.add(key)

    keys.discard("b")  # middle: last element moves into the hole
    keys.discard("e")  # last element
    keys.discard("zzz")  # missing: no-op

    assert sorted(keys) == ["a", "c", "d"]
    for key in ("a", "c", "d"):
        keys.discard(key)
    assert len(keys) == 0


def test_sample_never_exceeds_size() -> None:
    keys = SampleableKeySet()
    for i in range(10):
        keys.add(f"k{i}")
    rng = random.Random(1)

    assert len(keys.sample(3, rng)) == 3
    assert sorted(keys.sample(50, rng)) == sorted(keys)
    assert SampleableKeySet().sample(5, rng) == []
