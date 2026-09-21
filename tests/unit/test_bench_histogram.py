"""The load generator's log-linear latency histogram."""

import math
import random

import pytest

from benchmarks.histogram import LatencyHistogram, bucket_bounds, bucket_index


def exact_percentile(values: list[int], pct: float) -> int:
    ordered = sorted(values)
    rank = max(1, math.ceil(len(ordered) * pct / 100))
    return ordered[rank - 1]


@pytest.mark.parametrize("value", [0, 1, 255, 256, 257, 511, 512, 1000, 123_456, 7_000_000_000])
def test_every_value_falls_inside_its_bucket(value: int) -> None:
    low, high = bucket_bounds(bucket_index(value))
    assert low <= value <= high


def test_buckets_are_contiguous_and_ordered() -> None:
    previous_high = -1
    for index in range(3000):
        low, high = bucket_bounds(index)
        assert low == previous_high + 1
        assert high >= low
        previous_high = high


def test_relative_error_is_below_half_a_percent() -> None:
    for index in range(256, 4000, 37):
        low, high = bucket_bounds(index)
        assert (high - low) / 2 / low < 0.004


def test_percentiles_match_exact_computation() -> None:
    rng = random.Random(7)
    # Heavy-tailed, like real latencies: mostly ~200 us, a few ~100 ms.
    values = [int(rng.lognormvariate(12, 1.2)) for _ in range(50_000)]
    hist = LatencyHistogram()
    for value in values:
        hist.record(value)
    for pct in (0, 50, 90, 99, 99.9, 100):
        expected = exact_percentile(values, pct)
        assert hist.percentile(pct) == pytest.approx(expected, rel=0.005)
    assert hist.count == len(values)
    assert hist.min == min(values)
    assert hist.max == max(values)
    assert hist.mean == pytest.approx(sum(values) / len(values))


def test_merge_equals_recording_everything_in_one() -> None:
    rng = random.Random(3)
    values = [rng.randrange(1, 10**9) for _ in range(5_000)]
    whole, first, second = LatencyHistogram(), LatencyHistogram(), LatencyHistogram()
    for i, value in enumerate(values):
        whole.record(value)
        (first if i % 2 else second).record(value)
    first.merge(second)
    first.merge(LatencyHistogram())  # merging an empty histogram is a no-op
    assert first.to_dict() == whole.to_dict()


def test_weighted_record_counts_a_pipelined_batch() -> None:
    hist = LatencyHistogram()
    hist.record(1_000, count=16)
    assert hist.count == 16
    assert hist.percentile(50) == 1_000
    assert hist.mean == 1_000


def test_round_trips_through_a_dict() -> None:
    hist = LatencyHistogram()
    for value in (5, 900, 70_000, 3_000_000):
        hist.record(value)
    clone = LatencyHistogram.from_dict(hist.to_dict())
    assert clone.to_dict() == hist.to_dict()
    assert clone.percentile(75) == hist.percentile(75)


def test_empty_histogram_and_bad_percentile() -> None:
    hist = LatencyHistogram()
    assert hist.percentile(99) == 0
    assert hist.mean == 0
    assert hist.summary_us()["p99"] == 0
    with pytest.raises(ValueError, match="percentile"):
        hist.percentile(101)


def test_summary_is_in_microseconds() -> None:
    hist = LatencyHistogram()
    hist.record(250_000)  # 250 us
    summary = hist.summary_us((50, 99.9))
    assert set(summary) == {"p50", "p99.9", "mean", "max"}
    assert summary["p50"] == pytest.approx(250, rel=0.004)
    assert summary["max"] == 250.0
