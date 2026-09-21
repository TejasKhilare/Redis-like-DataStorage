"""What the load generator sends: the key distribution and the read/write mix."""

from __future__ import annotations

import bisect
import itertools
import random
from dataclasses import dataclass
from typing import Literal

Distribution = Literal["uniform", "zipf"]


@dataclass(frozen=True, slots=True)
class Workload:
    read_ratio: float = 0.9  # fraction of GETs; the rest are SETs
    keyspace: int = 100_000
    value_size: int = 64
    distribution: Distribution = "uniform"
    zipf_s: float = 0.99  # YCSB's default skew
    key_prefix: str = "key:"

    def __post_init__(self) -> None:
        if not 0 <= self.read_ratio <= 1:
            raise ValueError("read_ratio must be within [0, 1]")
        if self.keyspace < 1:
            raise ValueError("keyspace must be at least 1")
        if self.value_size < 1:
            raise ValueError("value_size must be at least 1")

    def key(self, index: int) -> bytes:
        return b"%s%012d" % (self.key_prefix.encode(), index)

    def value(self) -> bytes:
        return b"x" * self.value_size


class KeyChooser:
    """Draws key indexes in ``[0, keyspace)``, uniformly or Zipf-distributed.

    Zipf: key ``i`` (0-based rank) has weight ``1 / (i + 1) ** s``, so a few
    hot keys take most of the traffic, the way real caches are hit. The CDF is
    built once and sampled with a binary search. Hot ranks are scattered over
    the keyspace by a fixed permutation so that they don't all share a prefix.
    """

    def __init__(self, workload: Workload, rng: random.Random) -> None:
        self._rng = rng
        self._n = workload.keyspace
        self._cdf: list[float] | None = None
        self._permutation: list[int] | None = None
        if workload.distribution == "zipf":
            weights = (1.0 / (i + 1) ** workload.zipf_s for i in range(self._n))
            cdf = list(itertools.accumulate(weights))
            total = cdf[-1]
            self._cdf = [c / total for c in cdf]
            self._permutation = list(range(self._n))
            random.Random(0x5EED).shuffle(self._permutation)

    def next(self) -> int:
        if self._cdf is None:
            return self._rng.randrange(self._n)
        assert self._permutation is not None
        rank = bisect.bisect_left(self._cdf, self._rng.random())
        return self._permutation[min(rank, self._n - 1)]


class OperationStream:
    """An endless, seeded stream of (is_read, key) operations."""

    def __init__(self, workload: Workload, seed: int) -> None:
        self._rng = random.Random(seed)
        self._keys = KeyChooser(workload, self._rng)
        self._workload = workload

    def next(self) -> tuple[bool, bytes]:
        is_read = self._rng.random() < self._workload.read_ratio
        return is_read, self._workload.key(self._keys.next())
