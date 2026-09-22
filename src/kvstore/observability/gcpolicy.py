"""Keep CPython's cycle collector out of the latency path.

Every stored entry is a GC-tracked object, so a full (generation 2)
collection scans the whole keyspace: tens of milliseconds at 50k hashes,
with every command waiting. Copying a keyspace for a snapshot allocates
enough to trigger several in a row -- measured: 33, 45 and 51 ms stalls,
each collecting nothing.

* :func:`hold` / :func:`release` bracket a snapshot. The first hold calls
  ``gc.freeze()``, which moves every existing object to a permanent
  generation that collections skip; the last release moves them back, and
  any cyclic garbage created meanwhile is collected then. Holds nest: the
  interpreter has one collector.
* :func:`install_pause_metrics` times every collection, by generation, for
  ``/metrics``.
"""

from __future__ import annotations

import gc
import time
from typing import Any

from kvstore.observability.metrics import Histogram

_holds = 0
PAUSES: dict[int, Histogram] = {}
COLLECTED: dict[int, int] = {}
_started = 0.0
_installed = False


def hold() -> None:
    global _holds
    if _holds == 0:
        gc.freeze()
    _holds += 1


def release() -> None:
    global _holds
    if _holds == 0:
        return
    _holds -= 1
    if _holds == 0:
        gc.unfreeze()


def held() -> bool:
    return _holds > 0


def _callback(phase: str, info: dict[str, Any]) -> None:
    global _started
    if phase == "start":
        _started = time.perf_counter()
        return
    generation = int(info["generation"])
    histogram = PAUSES.get(generation)
    if histogram is None:
        histogram = PAUSES[generation] = Histogram()
    histogram.observe(time.perf_counter() - _started)
    COLLECTED[generation] = COLLECTED.get(generation, 0) + int(info["collected"])


def install_pause_metrics() -> None:
    """Time every collection (idempotent)."""
    global _installed
    if not _installed:
        gc.callbacks.append(_callback)
        _installed = True
