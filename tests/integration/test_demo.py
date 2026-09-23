"""The demo in the README: it runs, and its cluster really does survive the kill.

Seven real processes, so it is as slow as the recording. It is here because the
README shows what this script prints; if the output drifts, the recording lies.
"""

import re
import subprocess
import sys
from pathlib import Path

DEMO = Path(__file__).resolve().parents[2] / "scripts" / "demo.py"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def test_the_demo_survives_killing_a_primary() -> None:
    finished = subprocess.run(
        [sys.executable, str(DEMO), "--fast", "--writes", "500"],
        capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    output = ANSI.sub("", finished.stdout)
    assert finished.returncode == 0, output + finished.stderr

    assert re.search(r"SET user:1 tejas\s+-> g\d+ \(127\.0\.0\.1:65\d\d\)", output), output
    # Two keys, two groups, one command: the CROSSSLOT refusal.
    assert re.search(r"MSET \S+ 1 \S+ 2\s+Keys in request don't hash to the same shard", output)
    assert re.search(r"acknowledged writes\s+500", output), output
    assert re.search(r"failover\s+g1: 127\.0\.0\.1:6500 -> 127\.0\.0\.1:6503", output), output
    assert re.search(r"^  acknowledged writes lost\s+0$", output, re.M), output
    assert re.search(r"leaderboard top, from the promoted replica\s+\['tejas'\]", output), output
    assert "kvstore_cluster_failovers_total" in output, output
    assert "The group survived losing its primary." in output, output
