"""What the per-command metrics cost: one shard with ``KV_METRICS_ENABLED`` on and off.

    python -m benchmarks.metrics_overhead --redis-benchmark ~/redis/src/redis-benchmark \\
        --out benchmarks/results/phase5/metrics-overhead.json

redis-benchmark SET and GET against a single shard, unpipelined and with
16-deep pipelines (where per-command costs show most), metrics on and off
alternating within every repetition so drift on the machine hits both
equally. Reported: the median throughput of each, and the difference.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

from benchmarks.scaling import ATTEMPTS, CLIENTS, REQUESTS, finish, start_redis_benchmark
from benchmarks.servers import Deployment
from benchmarks.stallwatch import StallWatch


def run_once(
    redis_benchmark: str, enabled: bool, depth: int, work_dir: Path | None
) -> tuple[float, dict[str, Any]]:
    with Deployment(work_dir) as deployment, StallWatch() as watch:
        env = {"KV_METRICS_ENABLED": "true" if enabled else "false"}
        node = deployment.kvstore_node("node", env=env)
        total = 0.0
        for test in ("set", "get"):
            for _ in range(ATTEMPTS):
                result = finish(
                    start_redis_benchmark(
                        redis_benchmark,
                        node,
                        test,
                        clients=CLIENTS,
                        requests=REQUESTS[depth],
                        depth=depth,
                    )
                )
                if isinstance(result, float):
                    total += result
                    break
                print(f"  {test} failed, retrying: {result}", flush=True)
            else:
                raise RuntimeError(f"{test} failed {ATTEMPTS} times")
    return total / 2, watch.summary()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.metrics_overhead", description=__doc__
    )
    parser.add_argument("--redis-benchmark", required=True)
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    runs: dict[str, list[float]] = {}
    stalls: dict[str, list[dict[str, Any]]] = {}
    for rep in range(1, args.reps + 1):
        for depth in (1, 16):
            # Alternate which goes first, so neither always gets the warmer machine.
            order = (True, False) if rep % 2 else (False, True)
            for enabled in order:
                ops, stall = run_once(args.redis_benchmark, enabled, depth, args.work_dir)
                key = f"P{depth}-{'on' if enabled else 'off'}"
                runs.setdefault(key, []).append(round(ops))
                stalls.setdefault(key, []).append(stall)
                print(
                    f"rep {rep}: {key} {ops:,.0f} ops/s (mean of SET and GET), host stalls {stall}",
                    flush=True,
                )
    summary: list[dict[str, Any]] = []
    for depth in (1, 16):
        on = statistics.median(runs[f"P{depth}-on"])
        off = statistics.median(runs[f"P{depth}-off"])
        summary.append(
            {
                "pipeline": depth,
                "metrics_on_ops_per_sec": round(on),
                "metrics_off_ops_per_sec": round(off),
                "cost_percent": round((off - on) / off * 100, 1),
            }
        )
        print(json.dumps(summary[-1]), flush=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        report = {"summary": summary, "runs": runs, "host_stalls": stalls}
        args.out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
