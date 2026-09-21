"""A log-linear latency histogram in the style of HdrHistogram.

Values are integer nanoseconds. Every power-of-two range above 256 ns is
split into 128 equal buckets, so a recorded value is off by at most 1/256
of itself (< 0.4%) at any magnitude -- 50 us and 5 s alike -- in a few
thousand counters. Recording is O(1), and histograms from several worker
processes merge exactly by adding counts, which exact sample lists would
make expensive and averaged percentiles would make wrong.
"""

from __future__ import annotations

from typing import Any

SUB_BUCKET_BITS = 7
_SUB = 1 << SUB_BUCKET_BITS  # 128 buckets per power of two
_LINEAR = _SUB << 1  # values below 256 get one bucket each


def bucket_index(value: int) -> int:
    if value < _LINEAR:
        return value
    shift = value.bit_length() - (SUB_BUCKET_BITS + 1)
    return _LINEAR + (shift - 1) * _SUB + (value >> shift) - _SUB


def bucket_bounds(index: int) -> tuple[int, int]:
    """Smallest and largest value that land in bucket ``index``."""
    if index < _LINEAR:
        return index, index
    shift, offset = divmod(index - _LINEAR, _SUB)
    shift += 1
    top = offset + _SUB
    return top << shift, ((top + 1) << shift) - 1


class LatencyHistogram:
    __slots__ = ("_counts", "count", "max", "min", "total")

    def __init__(self) -> None:
        self._counts: dict[int, int] = {}
        self.count = 0
        self.total = 0
        self.min = 0
        self.max = 0

    def record(self, value_ns: int, count: int = 1) -> None:
        value_ns = max(value_ns, 0)
        index = bucket_index(value_ns)
        counts = self._counts
        counts[index] = counts.get(index, 0) + count
        if self.count == 0 or value_ns < self.min:
            self.min = value_ns
        if value_ns > self.max:
            self.max = value_ns
        self.count += count
        self.total += value_ns * count

    def merge(self, other: LatencyHistogram) -> None:
        if other.count == 0:
            return
        for index, count in other._counts.items():
            self._counts[index] = self._counts.get(index, 0) + count
        self.min = other.min if self.count == 0 else min(self.min, other.min)
        self.max = max(self.max, other.max)
        self.count += other.count
        self.total += other.total

    @property
    def mean(self) -> float:
        return self.total / self.count if self.count else 0.0

    def percentile(self, pct: float) -> int:
        """The value at or below which ``pct`` percent of recordings fall (nanoseconds)."""
        if not 0 <= pct <= 100:
            raise ValueError("percentile must be within [0, 100]")
        if self.count == 0:
            return 0
        rank = max(1, -(-self.count * pct // 100))  # ceil, and p0 is the first value
        seen = 0
        for index in sorted(self._counts):
            seen += self._counts[index]
            if seen >= rank:
                low, high = bucket_bounds(index)
                return min(max((low + high) // 2, self.min), self.max)
        return self.max  # pragma: no cover - rank never exceeds count

    def summary_us(self, percentiles: tuple[float, ...] = (50, 90, 99, 99.9)) -> dict[str, float]:
        """Percentiles, mean and max in microseconds (``p50``, ``p99.9``, ...)."""
        result = {f"p{pct:g}": round(self.percentile(pct) / 1000, 1) for pct in percentiles}
        result["mean"] = round(self.mean / 1000, 1)
        result["max"] = round(self.max / 1000, 1)
        return result

    # ---- transport between processes and into result files
    def to_dict(self) -> dict[str, Any]:
        return {
            "counts": sorted(self._counts.items()),
            "count": self.count,
            "total": self.total,
            "min": self.min,
            "max": self.max,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LatencyHistogram:
        hist = cls()
        hist._counts = {int(index): int(count) for index, count in data["counts"]}
        hist.count = int(data["count"])
        hist.total = int(data["total"])
        hist.min = int(data["min"])
        hist.max = int(data["max"])
        return hist
