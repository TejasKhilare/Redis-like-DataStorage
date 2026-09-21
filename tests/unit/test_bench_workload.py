"""The load generator's key distributions and operation mix."""

import random
from collections import Counter

import pytest

from benchmarks.workload import KeyChooser, OperationStream, Workload


def test_keys_are_fixed_width_and_distinct() -> None:
    workload = Workload(keyspace=1000)
    assert workload.key(7) == b"key:000000000007"
    assert len({workload.key(i) for i in range(1000)}) == 1000
    assert workload.value() == b"x" * 64


@pytest.mark.parametrize(
    "bad",
    [{"read_ratio": 1.5}, {"read_ratio": -0.1}, {"keyspace": 0}, {"value_size": 0}],
)
def test_invalid_workloads_are_rejected(bad: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="must be"):
        Workload(**bad)  # type: ignore[arg-type]


def test_read_ratio_is_respected() -> None:
    stream = OperationStream(Workload(read_ratio=0.9), seed=1)
    reads = sum(stream.next()[0] for _ in range(20_000))
    assert 0.88 < reads / 20_000 < 0.92


@pytest.mark.parametrize("ratio", [0.0, 1.0])
def test_pure_read_and_pure_write(ratio: float) -> None:
    stream = OperationStream(Workload(read_ratio=ratio), seed=1)
    assert {stream.next()[0] for _ in range(1000)} == {bool(ratio)}


def test_streams_are_reproducible_per_seed() -> None:
    workload = Workload(keyspace=500)
    a, b, c = (
        OperationStream(workload, 5),
        OperationStream(workload, 5),
        OperationStream(workload, 6),
    )
    first = [a.next() for _ in range(100)]
    assert first == [b.next() for _ in range(100)]
    assert first != [c.next() for _ in range(100)]


def test_uniform_keys_cover_the_keyspace() -> None:
    chooser = KeyChooser(Workload(keyspace=100), random.Random(1))
    counts = Counter(chooser.next() for _ in range(20_000))
    assert set(counts) == set(range(100))
    assert max(counts.values()) < 2 * min(counts.values())


def test_zipf_keys_are_skewed_and_in_range() -> None:
    chooser = KeyChooser(Workload(keyspace=10_000, distribution="zipf"), random.Random(1))
    counts = Counter(chooser.next() for _ in range(50_000))
    assert all(0 <= key < 10_000 for key in counts)
    hottest = sum(count for _, count in counts.most_common(100))
    # With s=0.99 the top 1% of keys takes roughly half of the traffic.
    assert hottest / 50_000 > 0.4
    # ...and the hot keys are scattered, not simply 0..99.
    assert {key for key, _ in counts.most_common(10)} != set(range(10))
