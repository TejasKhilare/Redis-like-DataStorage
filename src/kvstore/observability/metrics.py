"""Metrics that cost almost nothing to record, rendered for Prometheus when scraped.

Recording happens on every command, so it is plain Python: a counter is an
int, a histogram a list of bucket counts found with ``bisect`` -- about
0.8 us per command, against ~9 us for a labelled ``prometheus_client``
histogram (measured; more than the engine's own cost of a ``SET``). Values
that already exist elsewhere (key count, memory, replication offsets) are
not copied at all: :class:`Exposition` reads them when ``/metrics`` is
scraped.

Output is the Prometheus text format, version 0.0.4.
"""

from __future__ import annotations

import bisect
import math
import mmap
import os
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# Seconds: from 10 us (an in-memory GET) to 2.5 s (a stalled fsync).
LATENCY_BUCKETS: tuple[float, ...] = (
    1e-5, 2.5e-5, 5e-5, 1e-4, 2.5e-4, 5e-4, 1e-3, 2.5e-3, 5e-3,
    0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5,
)  # fmt: skip
SIZE_BUCKETS: tuple[float, ...] = (1, 2, 4, 8, 16, 32, 64, 128, 256, 512)

Labels = Mapping[str, str]


class Histogram:
    """Cumulative-bucket histogram. Thread-safe only when ``locked``."""

    __slots__ = ("_lock", "bounds", "buckets", "count", "sum")

    def __init__(self, bounds: Sequence[float] = LATENCY_BUCKETS, *, locked: bool = False) -> None:
        self.bounds = tuple(bounds)
        self.buckets = [0] * (len(self.bounds) + 1)  # the last one is +Inf
        self.count = 0
        self.sum = 0.0
        self._lock = threading.Lock() if locked else None

    def observe(self, value: float) -> None:
        if self._lock is None:
            self.buckets[bisect.bisect_left(self.bounds, value)] += 1
            self.count += 1
            self.sum += value
            return
        with self._lock:
            self.buckets[bisect.bisect_left(self.bounds, value)] += 1
            self.count += 1
            self.sum += value

    def cumulative(self) -> list[tuple[str, int]]:
        """``(le, count of observations <= le)`` for every bound, then ``+Inf``."""
        running, out = 0, []
        for bound, count in zip((*self.bounds, math.inf), self.buckets, strict=True):
            running += count
            out.append((_number(bound), running))
        return out


@dataclass(slots=True)
class CommandStats:
    """Per-command counts, errors and latency (Redis's ``INFO commandstats``, plus a histogram)."""

    enabled: bool = True
    calls: dict[str, int] = field(default_factory=dict)
    errors: dict[tuple[str, str], int] = field(default_factory=dict)
    latency: dict[str, Histogram] = field(default_factory=dict)

    def record(self, command: str, seconds: float) -> None:
        self.calls[command] = self.calls.get(command, 0) + 1
        histogram = self.latency.get(command)
        if histogram is None:
            histogram = self.latency[command] = Histogram()
        histogram.observe(seconds)

    def error(self, command: str, prefix: str) -> None:
        key = (command, prefix)
        self.errors[key] = self.errors.get(key, 0) + 1


class Exposition:
    """Builds one scrape: ``counter()``, ``gauge()``, ``histogram()`` families, then ``text()``.

    Names are full metric names; a counter's gets ``_total`` appended.
    """

    def __init__(self) -> None:
        self._lines: list[str] = []

    def counter(self, name: str, help_: str, samples: Iterable[tuple[Labels, float]]) -> None:
        self._family(name + "_total", help_, "counter", samples)

    def gauge(self, name: str, help_: str, samples: Iterable[tuple[Labels, float]]) -> None:
        self._family(name, help_, "gauge", samples)

    def histogram(self, name: str, help_: str, series: Iterable[tuple[Labels, Histogram]]) -> None:
        full = name
        series = list(series)
        if not series:
            return
        self._header(full, help_, "histogram")
        for labels, histogram in series:
            for le, count in histogram.cumulative():
                self._lines.append(f"{full}_bucket{_labels({**labels, 'le': le})} {count}")
            self._lines.append(f"{full}_sum{_labels(labels)} {_number(histogram.sum)}")
            self._lines.append(f"{full}_count{_labels(labels)} {histogram.count}")

    def text(self) -> str:
        return "\n".join(self._lines) + "\n"

    def _family(
        self, name: str, help_: str, kind: str, samples: Iterable[tuple[Labels, float]]
    ) -> None:
        rows = [f"{name}{_labels(labels)} {_number(value)}" for labels, value in samples]
        if rows:
            self._header(name, help_, kind)
            self._lines.extend(rows)

    def _header(self, name: str, help_: str, kind: str) -> None:
        self._lines.append(f"# HELP {name} {help_}")
        self._lines.append(f"# TYPE {name} {kind}")


def _labels(labels: Labels) -> str:
    if not labels:
        return ""
    body = ",".join(f'{key}="{_escape(str(value))}"' for key, value in labels.items())
    return "{" + body + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _number(value: float) -> str:
    if value == math.inf:
        return "+Inf"
    if value == -math.inf:
        return "-Inf"
    if isinstance(value, int) or float(value).is_integer():
        return str(int(value))
    return repr(float(value))


# ------------------------------------------------------------------ process
_STARTED = time.time()


def process_metrics(out: Exposition) -> None:
    """CPU, memory, file descriptors, start time, GC pauses."""
    from kvstore.observability import gcpolicy  # imports this module

    out.histogram(
        "kvstore_gc_pause_seconds",
        "CPython cycle-collector pauses, by generation (every command waits).",
        [({"generation": str(g)}, h) for g, h in sorted(gcpolicy.PAUSES.items())],
    )
    out.counter(
        "kvstore_gc_collected_objects",
        "Objects freed by the cycle collector, by generation.",
        [({"generation": str(g)}, n) for g, n in sorted(gcpolicy.COLLECTED.items())],
    )
    out.gauge("kvstore_gc_frozen", "1 while a snapshot keeps the GC frozen.",
              [({}, int(gcpolicy.held()))])  # fmt: skip
    out.counter(
        "process_cpu_seconds", "Total user and system CPU time.", [({}, time.process_time())]
    )
    out.gauge(
        "process_start_time_seconds", "Start time, seconds since the epoch.", [({}, _STARTED)]
    )
    rss = _resident_bytes()
    if rss is not None:
        out.gauge("process_resident_memory_bytes", "Resident memory size.", [({}, rss)])
    try:
        fds = len(os.listdir("/proc/self/fd"))
    except OSError:
        return
    out.gauge("process_open_fds", "Open file descriptors.", [({}, fds)])


def _resident_bytes() -> int | None:
    try:
        with open("/proc/self/statm", encoding="ascii") as fh:  # Linux
            return int(fh.read().split()[1]) * mmap.PAGESIZE
    except (OSError, ValueError):
        return None
