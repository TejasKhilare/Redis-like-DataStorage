"""Detect stalls of the whole machine while a benchmark runs.

A separate process sleeps 5 ms at a time; when it wakes much later than
that, nothing on the machine got to run -- in a VM, the host descheduled
it (WSL2 does, when Windows is busy or its virtual disk stalls). Such a stall
hits every server and client at once, so it is recorded next to each
measurement: a failed run or an outlier that coincides with one says more
about the machine than about the code.
"""

from __future__ import annotations

import subprocess
import sys
from contextlib import AbstractContextManager
from types import TracebackType
from typing import Any, Self

_TICKER = """
import sys, time
threshold = float(sys.argv[1])
last = time.monotonic()
while True:
    time.sleep(0.005)
    now = time.monotonic()
    if now - last > threshold:
        print(round((now - last) * 1000, 1), flush=True)
    last = now
"""


class StallWatch(AbstractContextManager["StallWatch"]):
    """``with StallWatch() as watch: ...`` then ``watch.summary()``."""

    def __init__(self, threshold_s: float = 0.1) -> None:
        self.threshold_s = threshold_s
        self.stalls_ms: list[float] = []
        self._proc: subprocess.Popen[str] | None = None

    def __enter__(self) -> Self:
        self._proc = subprocess.Popen(
            [sys.executable, "-c", _TICKER, str(self.threshold_s)],
            stdout=subprocess.PIPE,
            text=True,
        )
        return self

    @property
    def pid(self) -> int:
        assert self._proc is not None
        return self._proc.pid

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._proc is not None
        self._proc.terminate()
        out, _ = self._proc.communicate(timeout=10)
        self.stalls_ms = [float(line) for line in out.split()]

    def summary(self) -> dict[str, Any]:
        """Stalls longer than the threshold: how many, the longest, and their total."""
        return {
            "count": len(self.stalls_ms),
            "max_ms": max(self.stalls_ms, default=0.0),
            "total_ms": round(sum(self.stalls_ms), 1),
        }
