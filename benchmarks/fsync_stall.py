"""How long saving a small file (write + fsync + rename) takes while other files are written.

    python -m benchmarks.fsync_stall --out benchmarks/results/phase5/fsync-stall.json

The cluster manager saves ``cluster.json`` during a failover; this measures
what that costs on the machine's disk, and why it must not run on an event
loop (ADR-0015). ``ClusterConfig.save`` every 50 ms, in three conditions:

* **idle**;
* **7 AOF-like writers**: threads appending 4 KB at ~1 MB/s each and
  fsyncing once a second (``appendfsync everysec``) -- the seven nodes of
  docker-compose.yml, each near its write capacity (~8k SETs/s);
* **one unthrottled writer**: appending 1 MB chunks as fast as the disk
  takes them, fsyncing once a second -- a bulk copy, say, or a snapshot of a
  large keyspace being written.

Each condition runs for up to 60 saves or 90 s, whichever comes first.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from kvstore.cluster.topology import ClusterConfig

CONFIG = ClusterConfig.from_spec(["a=127.0.0.1:1+127.0.0.1:2", "b=127.0.0.1:3+127.0.0.1:4"])


def _saves(path: Path, samples: int = 60, budget_s: float = 90.0) -> list[float]:
    times: list[float] = []
    deadline = time.monotonic() + budget_s
    while len(times) < samples and time.monotonic() < deadline:
        started = time.perf_counter()
        CONFIG.save(path)
        times.append((time.perf_counter() - started) * 1000)
        time.sleep(0.05)
    return times


def _writer(path: Path, stop: threading.Event, *, chunk: int, rate_bytes: float | None) -> None:
    data = os.urandom(chunk)
    with path.open("wb") as fh:
        last_fsync = time.monotonic()
        while not stop.is_set():
            fh.write(data)
            if rate_bytes:
                time.sleep(chunk / rate_bytes)
            if time.monotonic() - last_fsync >= 1.0:
                fh.flush()
                os.fsync(fh.fileno())
                last_fsync = time.monotonic()
                if fh.tell() > 256 * 1024 * 1024:  # bound the disk used
                    fh.seek(0)
                    fh.truncate()


def _condition(work: Path, writers: list[Callable[[threading.Event], None]]) -> dict[str, Any]:
    stop = threading.Event()
    threads = [threading.Thread(target=w, args=(stop,), daemon=True) for w in writers]
    for thread in threads:
        thread.start()
    time.sleep(1.0 if writers else 0)  # let the writers build up dirty pages
    try:
        times = sorted(_saves(work / "cluster.json"))
    finally:
        stop.set()
        for thread in threads:
            thread.join()
    return {
        "saves": len(times),
        "median_ms": round(statistics.median(times), 1),
        "p90_ms": round(times[int(len(times) * 0.9)], 1),
        "max_ms": round(times[-1], 1),
    }


def run(work_dir: Path | None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="kvbench-fsync-", dir=work_dir) as tmp:
        work = Path(tmp)
        aof_like: list[Callable[[threading.Event], None]] = [
            partial(_writer, work / f"aof-{i}", chunk=4096, rate_bytes=1024 * 1024)
            for i in range(7)
        ]
        bulk: list[Callable[[threading.Event], None]] = [
            partial(_writer, work / "bulk", chunk=1024 * 1024, rate_bytes=None)
        ]
        report = {}
        for name, writers in (("idle", []), ("aof_like_writers", aof_like), ("bulk_writer", bulk)):
            report[name] = _condition(work, writers)
            print(name, report[name], flush=True)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m benchmarks.fsync_stall", description=__doc__)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    report = run(args.work_dir)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
