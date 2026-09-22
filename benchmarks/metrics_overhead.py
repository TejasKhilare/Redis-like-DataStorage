"""What the per-command metrics cost: one shard with ``KV_METRICS_ENABLED`` on and off.

    python -m benchmarks.metrics_overhead --redis-benchmark ~/redis/src/redis-benchmark \\
        --out benchmarks/results/phase5/metrics-overhead.json

redis-benchmark SET and GET against a single shard, unpipelined and with
16-deep pipelines (where per-command costs show most), metrics on and off
alternating within every repetition so drift on the machine hits both
equally.

The cost is reported as the **server's CPU time per request** (from
``/proc``), with throughput next to it. On a noisy machine the two can
disagree: one comparison measured 2.5 µs more CPU per request (+9%) and
yet 16% less throughput. Scheduling noise lands on throughput, not on the
CPU a request consumes.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

from benchmarks.scaling import (
    ATTEMPTS,
    CLIENTS,
    REQUESTS,
    cpu_seconds,
    finish,
    start_redis_benchmark,
)
from benchmarks.servers import Deployment
from benchmarks.stallwatch import StallWatch


def run_once(
    redis_benchmark: str, enabled: bool, depth: int, work_dir: Path | None
) -> dict[str, Any]:
    with Deployment(work_dir) as deployment, StallWatch() as watch:
        env = {"KV_METRICS_ENABLED": "true" if enabled else "false"}
        node = deployment.kvstore_node("node", env=env)
        pid = deployment.pid("node")
        ops, cpu = 0.0, 0.0
        for test in ("set", "get"):
            for _ in range(ATTEMPTS):
                before = cpu_seconds(pid)
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
                    ops += result
                    cpu += cpu_seconds(pid) - before
                    break
                print(f"  {test} failed, retrying: {result}", flush=True)
            else:
                raise RuntimeError(f"{test} failed {ATTEMPTS} times")
    return {
        "ops_per_sec": round(ops / 2),
        "cpu_us_per_request": round(cpu / (2 * REQUESTS[depth]) * 1e6, 3),
        "host_stalls": watch.summary(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.metrics_overhead", description=__doc__
    )
    parser.add_argument("--redis-benchmark", required=True)
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    runs: dict[str, list[dict[str, Any]]] = {}
    for rep in range(1, args.reps + 1):
        for depth in (1, 16):
            # Alternate which goes first, so neither always gets the warmer machine.
            order = (True, False) if rep % 2 else (False, True)
            for enabled in order:
                run = run_once(args.redis_benchmark, enabled, depth, args.work_dir)
                key = f"P{depth}-{'on' if enabled else 'off'}"
                runs.setdefault(key, []).append(run)
                print(f"rep {rep}: {key} {json.dumps(run)}", flush=True)
    summary: list[dict[str, Any]] = []
    for depth in (1, 16):
        entry: dict[str, Any] = {"pipeline": depth}
        for field in ("cpu_us_per_request", "ops_per_sec"):
            on = statistics.median(r[field] for r in runs[f"P{depth}-on"])
            off = statistics.median(r[field] for r in runs[f"P{depth}-off"])
            entry[f"{field}_on"], entry[f"{field}_off"] = on, off
        cpu_on, cpu_off = entry["cpu_us_per_request_on"], entry["cpu_us_per_request_off"]
        entry["cpu_cost_us"] = round(cpu_on - cpu_off, 3)
        entry["cpu_cost_percent"] = round((cpu_on - cpu_off) / cpu_off * 100, 1)
        summary.append(entry)
        print(json.dumps(entry), flush=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        report = {"summary": summary, "runs": runs}
        args.out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
