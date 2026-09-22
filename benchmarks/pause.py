"""How long BGREWRITEAOF stalls the event loop, by keyspace size.

    python -m benchmarks.pause                       # 10k, 100k and 1M keys
    python -m benchmarks.pause --merge-into benchmarks/results/run.json

Without fork(), a rewrite has to copy the keyspace while no command runs.
Two measurements per size:

* **copy**: the one-shot copy on its own (``Store.snapshot()``) and the time
  the background thread then takes to write the snapshot -- the Phase 3
  numbers;
* **stall**: a running event loop with a ticker coroutine records the
  longest gap between its turns -- how long any command would have waited --
  while a whole rewrite runs, once with the one-shot copy and once with the
  incremental copy (slices between commands, copy-on-write barrier). The gap
  includes the GIL contention from the snapshot-writer thread, which clients
  feel too.

Each size is measured several times and the median reported.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from benchmarks.stallwatch import StallWatch
from kvstore.engine import Engine
from kvstore.engine.cron import run_cron

_BATCH = 1000


def _fill(engine: Engine, keys: int, kind: str, value: str) -> None:
    for start in range(0, keys, _BATCH):
        end = min(start + _BATCH, keys)
        if kind == "string":
            args: list[str] = []
            for i in range(start, end):
                args += (f"key:{i:012d}", value)
            engine.execute("MSET", *args)
        else:  # a hash of 10 fields per key
            for i in range(start, end):
                fields = [part for f in range(10) for part in (f"f{f}", value[:6])]
                engine.execute("HSET", f"key:{i:012d}", *fields)


async def _stall_during_rewrite(engine: Engine, *, incremental: bool) -> tuple[float, float]:
    """``(longest event-loop gap, whole rewrite)`` in ms, with the cron loop driving it."""
    persistence = engine.persistence
    assert persistence is not None
    engine.incremental_snapshots = incremental
    longest = 0.0
    running = True

    async def ticker() -> None:
        nonlocal longest
        last = time.perf_counter()
        while running:
            await asyncio.sleep(0)
            now = time.perf_counter()
            longest = max(longest, now - last)
            last = now

    cron = asyncio.create_task(run_cron(engine, interval_s=0.01, expiry_sample_size=20))
    tick = asyncio.create_task(ticker())
    await asyncio.sleep(0.05)
    longest = 0.0
    started = time.perf_counter()
    engine.start_rewrite()  # the one-shot copy blocks right here
    while persistence.rewrite_in_progress:  # noqa: ASYNC110 - the cron finishes it; no event
        await asyncio.sleep(0.005)
    whole = time.perf_counter() - started
    running = False
    await tick
    cron.cancel()
    with suppress(asyncio.CancelledError):
        await cron
    return round(longest * 1000, 2), round(whole * 1000, 1)


def measure(
    keys: int, kind: str = "string", *, reps: int = 3, value_size: int = 64
) -> dict[str, Any]:
    pauses, writes, stalls, stalls_inc, rewrites_inc = [], [], [], [], []
    host_stalls: list[dict[str, Any]] = []  # the machine's, during the incremental runs
    with (
        tempfile.TemporaryDirectory(prefix="kvbench-pause-") as tmp,
        Engine(data_dir=Path(tmp), aof_fsync="no", aof_rewrite_percentage=0) as engine,
    ):
        _fill(engine, keys, kind, "x" * value_size)
        persistence = engine.persistence
        assert persistence is not None
        for _ in range(reps):
            # One-shot copy, then wait for the writer and finalize. Set it here:
            # the stall measurements below switch incremental copying on, and
            # left on, the next repetition's "one-shot" copy was incremental.
            engine.incremental_snapshots = False
            engine.save()
            stats = persistence.stats()
            assert stats.last_snapshot_pause_ms is not None
            assert stats.last_rewrite_duration_ms is not None
            pauses.append(stats.last_snapshot_pause_ms)
            writes.append(stats.last_rewrite_duration_ms)
            stall, _ = asyncio.run(_stall_during_rewrite(engine, incremental=False))
            stalls.append(stall)
            with StallWatch() as watch:
                stall, whole = asyncio.run(_stall_during_rewrite(engine, incremental=True))
            host_stalls.append(watch.summary())
            stalls_inc.append(stall)
            rewrites_inc.append(whole)
        size = persistence.stats().aof_base_size
    return {
        "keys": keys,
        "kind": kind,
        "reps": reps,
        "pause_ms": round(statistics.median(pauses), 2),
        "pause_ms_max": round(max(pauses), 2),
        "background_write_ms": round(statistics.median(writes), 1),
        "snapshot_bytes": size,
        "stall_ms_oneshot": round(statistics.median(stalls), 2),
        "stall_ms_incremental": round(statistics.median(stalls_inc), 2),
        "stall_ms_incremental_max": round(max(stalls_inc), 2),
        "rewrite_ms_incremental": round(statistics.median(rewrites_inc), 1),
        "stalls_ms_incremental": stalls_inc,
        "host_stalls_incremental": host_stalls,
    }


def run(sizes: list[int], *, reps: int = 3) -> list[dict[str, Any]]:
    rows = []
    for kind in ("string", "hash"):
        for keys in sizes:
            if kind == "hash" and keys > 100_000:
                continue  # 1M hashes x 10 fields needs several GB in CPython
            row = measure(keys, kind, reps=reps)
            print(
                f"{kind:>6} x {keys:>9,}: copy {row['pause_ms']:>8,.1f} ms, "
                f"write {row['background_write_ms']:>7,.0f} ms | longest stall: "
                f"one-shot {row['stall_ms_oneshot']:>8,.1f} ms, "
                f"incremental {row['stall_ms_incremental']:>6,.1f} ms "
                f"(whole rewrite {row['rewrite_ms_incremental']:,.0f} ms)",
                flush=True,
            )
            rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m benchmarks.pause", description=__doc__)
    p.add_argument("--sizes", default="10000,100000,1000000", help="comma-separated key counts")
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--merge-into", type=Path, help="add the rows to a suite result file")
    p.add_argument("--out", type=Path, help="write the rows to their own JSON file")
    args = p.parse_args(argv)
    rows = run([int(size) for size in args.sizes.split(",")], reps=args.reps)
    if args.merge_into:
        data = json.loads(args.merge_into.read_text(encoding="utf-8"))
        data["snapshot_pause"] = rows
        args.merge_into.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        print(f"added to {args.merge_into}")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
