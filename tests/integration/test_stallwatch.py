"""benchmarks.stallwatch sees a machine that stops running anything."""

import os
import signal
import sys
import time

import pytest

from benchmarks.stallwatch import StallWatch


def test_a_quiet_machine_has_no_stalls() -> None:
    # A whole second: a busy CI machine may deschedule the ticker briefly,
    # and only a real freeze should count.
    with StallWatch(threshold_s=1.0) as watch:
        time.sleep(1.5)
    assert watch.summary() == {"count": 0, "max_ms": 0.0, "total_ms": 0}


@pytest.mark.skipif(sys.platform == "win32", reason="SIGSTOP is POSIX")
def test_a_frozen_ticker_is_reported_as_one_stall() -> None:
    # A stopped ticker is what a VM whose vCPUs the host descheduled looks like to it.
    with StallWatch(threshold_s=0.1) as watch:
        time.sleep(0.3)  # let it start ticking
        os.kill(watch.pid, signal.SIGSTOP)
        time.sleep(0.4)
        os.kill(watch.pid, signal.SIGCONT)
        time.sleep(0.2)
    summary = watch.summary()
    assert summary["count"] == 1
    assert 350 <= summary["max_ms"] < 1000
