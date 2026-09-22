"""Throughput with 1, 3 and 6 shards, and where the CPU goes.

    python -m benchmarks.scaling --redis-benchmark ~/redis/src/redis-benchmark \\
        --out benchmarks/results/phase5/scaling.json

Two ways to use N shards (no replicas, ``appendfsync everysec``), driven by
redis-benchmark (the C client, so the load generator costs little CPU):

* **router** -- every client talks to one router, which splits each
  pipeline by shard group;
* **direct** -- clients split evenly over the shards and talk to them
  directly, as a cluster-aware client would (the shards' aggregate
  capacity, with no router in the way).

Each point runs SET then GET (100-byte values, 100k-key space), unpipelined
and with 16-deep pipelines, repeated with the repetitions interleaved.
Alongside the throughput, the CPU time each process used over the run (from
``/proc/<pid>/stat``), in cores: on a machine with few cores this -- not the
design -- decides what adding shards can buy, so it is reported, not assumed.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from benchmarks.servers import Deployment, Endpoint
from benchmarks.stallwatch import StallWatch

SHARDS = (1, 3, 6)
CLIENTS = 48  # divisible by every shard count
REQUESTS = {1: 100_000, 16: 400_000}  # per test, by pipeline depth
ATTEMPTS = 3  # per test: redis-benchmark gives up on the first error reply


def cpu_seconds(pid: int) -> float:
    """User + system CPU time of a running process."""
    if sys.platform == "win32":
        raise SystemExit("benchmarks.scaling reads /proc: run it on Linux")
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    utime, stime = int(fields[11]), int(fields[12])
    ticks: int = os.sysconf("SC_CLK_TCK")
    return (utime + stime) / ticks


def _children_cpu() -> float:
    """CPU used by finished child processes (the redis-benchmark runs)."""
    if sys.platform == "win32":
        raise SystemExit("benchmarks.scaling reads /proc: run it on Linux")
    import resource

    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def start_redis_benchmark(
    redis_benchmark: str, endpoint: Endpoint, test: str, *, clients: int, requests: int, depth: int
) -> subprocess.Popen[str]:
    argv = [
        redis_benchmark,
        "-h", endpoint.host, "-p", str(endpoint.resp_port),
        "-t", test, "-n", str(requests), "-c", str(clients),
        "-r", "100000", "-d", "100", "-P", str(depth), "--csv",
    ]  # fmt: skip
    return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def finish(proc: subprocess.Popen[str]) -> float | str:
    """Ops/sec of a finished run, or why it produced none.

    redis-benchmark exits on the first error reply (a request that timed out
    through the router, say), printing no result: that is reported, not
    counted as zero.
    """
    out, err = proc.communicate()
    rows = list(csv.DictReader(io.StringIO(out)))
    if rows:
        return sum(float(record["rps"]) for record in rows)
    lines = [ln for ln in err.splitlines() if ln.strip() and "Could not fetch" not in ln]
    return lines[-1] if lines else f"no result (exit {proc.returncode})"


def run_point(
    redis_benchmark: str, shards: int, mode: str, depth: int, work_dir: Path | None
) -> dict[str, Any]:
    with Deployment(work_dir) as deployment:
        router = deployment.kvstore_cluster(shards)
        names = [f"shard-{i}" for i in range(1, shards + 1)]
        if mode == "router":
            targets = [router]
            names.append("router")
        else:
            targets = [deployment.endpoints[name] for name in names]
        pids = {name: deployment.pid(name) for name in names}
        row: dict[str, Any] = {"shards": shards, "mode": mode, "pipeline": depth}
        for test in ("set", "get"):
            row[f"{test}_ops_per_sec"] = None
            row[f"{test}_failures"] = []
            for _ in range(ATTEMPTS):
                before = {name: cpu_seconds(pid) for name, pid in pids.items()}
                client_before = _children_cpu()
                started = time.perf_counter()
                with StallWatch() as watch:
                    procs = [
                        start_redis_benchmark(
                            redis_benchmark,
                            target,
                            test,
                            clients=CLIENTS // len(targets),
                            requests=REQUESTS[depth] // len(targets),
                            depth=depth,
                        )
                        for target in targets
                    ]
                    results = [finish(proc) for proc in procs]
                wall = time.perf_counter() - started
                row.setdefault(f"{test}_host_stalls", []).append(watch.summary())
                failed = [r for r in results if isinstance(r, str)]
                if failed:
                    row[f"{test}_failures"] += failed
                    continue
                row[f"{test}_ops_per_sec"] = round(sum(r for r in results if isinstance(r, float)))
                cores = {
                    name: round((cpu_seconds(pid) - before[name]) / wall, 3)
                    for name, pid in pids.items()
                }
                cores["redis-benchmark"] = round((_children_cpu() - client_before) / wall, 3)
                row[f"{test}_cpu_cores"] = cores
                break
    return row


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    points: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
    for row in rows:
        points.setdefault((row["mode"], row["pipeline"], row["shards"]), []).append(row)
    summary = []
    for (mode, depth, shards), reps in sorted(points.items()):
        entry: dict[str, Any] = {"mode": mode, "pipeline": depth, "shards": shards}
        for test in ("set", "get"):
            done = [r for r in reps if r[f"{test}_ops_per_sec"] is not None]
            entry[f"{test}_failed_attempts"] = sum(len(r[f"{test}_failures"]) for r in reps)
            if not done:
                entry[f"{test}_ops_per_sec"] = None
                continue
            values = [r[f"{test}_ops_per_sec"] for r in done]
            entry[f"{test}_ops_per_sec"] = round(statistics.median(values))
            entry[f"{test}_min_max"] = [min(values), max(values)]
            median_rep = sorted(done, key=lambda r: r[f"{test}_ops_per_sec"])[len(done) // 2]
            entry[f"{test}_cpu_cores"] = median_rep[f"{test}_cpu_cores"]
        summary.append(entry)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m benchmarks.scaling", description=__doc__)
    parser.add_argument("--redis-benchmark", required=True)
    parser.add_argument("--shards", type=int, nargs="+", default=list(SHARDS))
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    rows = []
    for rep in range(1, args.reps + 1):  # interleaved: rep 1 of every point, then rep 2 ...
        for mode in ("router", "direct"):
            for depth in (1, 16):
                for shards in args.shards:
                    row = run_point(args.redis_benchmark, shards, mode, depth, args.work_dir)
                    print(f"rep {rep}: {json.dumps(row)}", flush=True)
                    rows.append(row)
    summary = summarize(rows)
    for entry in summary:
        print(json.dumps(entry), flush=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        report = {"clients": CLIENTS, "requests": REQUESTS, "summary": summary, "runs": rows}
        args.out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
