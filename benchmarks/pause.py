"""How long BGREWRITEAOF stalls the event loop, by keyspace size.

    python -m benchmarks.pause                       # 10k, 100k and 1M keys
    python -m benchmarks.pause --merge-into benchmarks/results/run.json

Without fork(), a rewrite starts by copying the keyspace on the event loop
(ADR-0006); no command runs during that copy. This measures the copy (the
*pause*) and the time the background thread then takes to write the
snapshot, in-process, for string keys and for hashes. Each size is
measured several times and the median reported.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from pathlib import Path
from typing import Any

from kvstore.engine import Engine

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


def measure(
    keys: int, kind: str = "string", *, reps: int = 3, value_size: int = 64
) -> dict[str, Any]:
    pauses, writes = [], []
    with (
        tempfile.TemporaryDirectory(prefix="kvbench-pause-") as tmp,
        Engine(data_dir=Path(tmp), aof_fsync="no", aof_rewrite_percentage=0) as engine,
    ):
        _fill(engine, keys, kind, "x" * value_size)
        persistence = engine.persistence
        assert persistence is not None
        for _ in range(reps):
            engine.save()  # start_rewrite(), then wait for the writer and finalize
            stats = persistence.stats()
            assert stats.last_snapshot_pause_ms is not None
            assert stats.last_rewrite_duration_ms is not None
            pauses.append(stats.last_snapshot_pause_ms)
            writes.append(stats.last_rewrite_duration_ms)
        size = persistence.stats().aof_base_size
    return {
        "keys": keys,
        "kind": kind,
        "reps": reps,
        "pause_ms": round(statistics.median(pauses), 2),
        "pause_ms_max": round(max(pauses), 2),
        "background_write_ms": round(statistics.median(writes), 1),
        "snapshot_bytes": size,
    }


def run(sizes: list[int], *, reps: int = 3) -> list[dict[str, Any]]:
    rows = []
    for kind in ("string", "hash"):
        for keys in sizes:
            if kind == "hash" and keys > 100_000:
                continue  # 1M hashes x 10 fields needs several GB in CPython
            row = measure(keys, kind, reps=reps)
            print(
                f"{kind:>6} x {keys:>9,}: pause {row['pause_ms']:>9,.1f} ms "
                f"(max {row['pause_ms_max']:,.1f}), "
                f"background write {row['background_write_ms']:>7,.0f} ms, "
                f"snapshot {row['snapshot_bytes'] / 1e6:,.1f} MB",
                flush=True,
            )
            rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m benchmarks.pause", description=__doc__)
    p.add_argument("--sizes", default="10000,100000,1000000", help="comma-separated key counts")
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--merge-into", type=Path, help="add the rows to a suite result file")
    args = p.parse_args(argv)
    rows = run([int(size) for size in args.sizes.split(",")], reps=args.reps)
    if args.merge_into:
        data = json.loads(args.merge_into.read_text(encoding="utf-8"))
        data["snapshot_pause"] = rows
        args.merge_into.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        print(f"added to {args.merge_into}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
