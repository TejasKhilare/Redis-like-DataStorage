"""The Phase 3 benchmark suite: every scenario, against kvstore and Redis, repeated.

    python -m benchmarks.suite --redis-server ~/redis/src/redis-server \\
        --redis-benchmark ~/redis/src/redis-benchmark --out benchmarks/results/run.json

Each run gets a freshly started server with an empty data directory and a
preloaded keyspace. Repetitions are *interleaved* (rep 1 of every scenario,
then rep 2 of every scenario, ...) rather than back to back, so a slow drift
in background load on the machine spreads over all scenarios instead of
skewing whichever ran during it. Throughput is reported as the median of the
repetitions with the min-max spread; latency percentiles come from the
repetitions' merged histograms.

After the closed-loop matrix, an open-loop sweep offers a fixed request rate
at fractions of each server's measured capacity -- the latency-vs-load curve.
Without ``--redis-server`` the Redis scenarios are skipped.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import kvstore
from benchmarks.drivers import Protocol
from benchmarks.histogram import LatencyHistogram
from benchmarks.load_gen import LoadConfig, LoadResult, format_result, preload, run, run_event_loop
from benchmarks.servers import Deployment, Endpoint
from benchmarks.workload import Workload

Target = Literal["kvstore", "redis", "router"]
FsyncPolicy = Literal["always", "everysec", "no"]

LADDER = (50, 75, 90, 99, 99.9, 99.99)


@dataclass(frozen=True, slots=True)
class Scenario:
    group: str
    target: Target
    protocol: Protocol = "resp"
    fsync: FsyncPolicy = "everysec"
    read_ratio: float = 0.9
    pipeline: int = 1

    @property
    def name(self) -> str:
        reads = round(self.read_ratio * 100)
        return f"{self.target}/{self.protocol} fsync={self.fsync} get={reads}% P={self.pipeline}"


def scenarios(*, with_redis: bool) -> list[Scenario]:
    """The matrix. Shared baseline: 50 clients, 90% GET, fsync everysec, no pipelining."""
    targets: list[Target] = ["kvstore", "redis"] if with_redis else ["kvstore"]
    result: list[Scenario] = []

    def add(scenario: Scenario) -> None:
        wanted = scenario.target in targets or scenario.target == "router"
        if wanted and scenario not in result:
            result.append(scenario)

    for target in targets:
        # RESP vs HTTP (and vs Redis), the baseline workload.
        add(Scenario("protocol", target))
        # Durability cost: write-only, each fsync policy; then group commit.
        for fsync in ("always", "everysec", "no"):
            add(Scenario("fsync", target, fsync=fsync, read_ratio=0.0))
        add(Scenario("group-commit", target, fsync="always", read_ratio=0.0, pipeline=16))
        # Pipelining (depth 1 is the baseline scenario above).
        for depth in (4, 16, 64):
            add(Scenario("pipeline", target, pipeline=depth))
        # Read-heavy to write-heavy.
        for reads in (0.95, 0.5, 0.05):
            add(Scenario("workload", target, read_ratio=reads))
    add(Scenario("protocol", "kvstore", protocol="http"))
    add(Scenario("router", "router"))
    add(Scenario("router", "router", pipeline=16))
    return result


# ------------------------------------------------------------ deployment
@dataclass(frozen=True, slots=True)
class SuiteOptions:
    clients: int = 50
    http_clients: int = 50
    duration_s: float = 8.0
    warmup_s: float = 2.0
    reps: int = 3
    workload: Workload = field(default_factory=Workload)
    redis_server: str | None = None
    redis_benchmark: str | None = None
    work_dir: Path | None = None
    curve_fractions: tuple[float, ...] = (0.2, 0.4, 0.6, 0.8, 0.9, 1.0, 1.2)
    curve_duration_s: float = 8.0


@contextmanager
def deploy(target: Target, fsync: FsyncPolicy, options: SuiteOptions) -> Iterator[Endpoint]:
    with Deployment(options.work_dir) as deployment:
        if target == "kvstore":
            endpoint = deployment.kvstore_node("shard", fsync=fsync)
        elif target == "router":
            endpoint = deployment.kvstore_cluster(3, fsync=fsync)
        else:
            assert options.redis_server is not None
            endpoint = deployment.redis(options.redis_server, fsync=fsync)
        # So GETs hit. Preload is pipelined, so even under fsync=always it
        # costs one fsync per 500 SETs rather than one per key.
        run_event_loop(lambda: preload(endpoint.host, endpoint.resp_port, options.workload))
        yield endpoint


def load_config(scenario: Scenario, endpoint: Endpoint, options: SuiteOptions) -> LoadConfig:
    http = scenario.protocol == "http"
    port = endpoint.http_port if http else endpoint.resp_port
    assert port is not None
    return LoadConfig(
        host=endpoint.host,
        port=port,
        protocol=scenario.protocol,
        clients=options.http_clients if http else options.clients,
        pipeline=scenario.pipeline,
        duration_s=options.duration_s,
        warmup_s=options.warmup_s,
        workload=replace(options.workload, read_ratio=scenario.read_ratio),
    )


def run_scenario(scenario: Scenario, options: SuiteOptions, *, rate: float = 0) -> LoadResult:
    with deploy(scenario.target, scenario.fsync, options) as endpoint:
        config = load_config(scenario, endpoint, options)
        if rate:
            config = replace(config, rate=rate, duration_s=options.curve_duration_s)
        return run(config)


# -------------------------------------------------------------- summary
def summarize(scenario: Scenario, results: list[LoadResult]) -> dict[str, Any]:
    merged = LatencyHistogram()
    for result in results:
        merged.merge(result.histogram)
    throughput = [r.ops_per_sec for r in results]
    return {
        "scenario": scenario.name,
        **asdict(scenario),
        "reps": len(results),
        "ops_per_sec": round(statistics.median(throughput), 1),
        "ops_per_sec_min": round(min(throughput), 1),
        "ops_per_sec_max": round(max(throughput), 1),
        "errors": sum(r.errors for r in results),
        "latency_us": merged.summary_us(LADDER),
        "histogram": merged.to_dict(),
    }


def run_matrix(matrix: list[Scenario], options: SuiteOptions) -> list[dict[str, Any]]:
    runs: dict[Scenario, list[LoadResult]] = {s: [] for s in matrix}
    total = len(matrix) * options.reps
    done = 0
    for rep in range(options.reps):
        for scenario in matrix:
            done += 1
            print(f"[{done}/{total}] rep {rep + 1}: {scenario.name}", flush=True)
            result = run_scenario(scenario, options)
            print(format_result(result), flush=True)
            runs[scenario].append(result)
    return [summarize(s, results) for s, results in runs.items()]


def run_latency_curve(summary: list[dict[str, Any]], options: SuiteOptions) -> list[dict[str, Any]]:
    """Open-loop sweep of the baseline scenario at fractions of measured capacity."""
    points = []
    for row in summary:
        if row["group"] != "protocol" or row["protocol"] != "resp":
            continue
        scenario = Scenario("protocol", row["target"])
        capacity = row["ops_per_sec"]
        for fraction in options.curve_fractions:
            rate = round(capacity * fraction)
            print(f"[curve] {scenario.target}: {rate:,} ops/s offered ({fraction:.0%})", flush=True)
            result = run_scenario(scenario, options, rate=rate)
            print(format_result(result), flush=True)
            points.append(
                {
                    "target": scenario.target,
                    "fraction": fraction,
                    "offered_ops_per_sec": rate,
                    "achieved_ops_per_sec": round(result.ops_per_sec, 1),
                    "errors": result.errors,
                    "latency_us": result.histogram.summary_us(LADDER),
                }
            )
    return points


# ------------------------------------------------------ redis-benchmark
def run_redis_benchmark(options: SuiteOptions) -> list[dict[str, Any]]:
    """redis-benchmark (the C client) against kvstore and Redis, with and without pipelining."""
    assert options.redis_benchmark is not None
    targets: list[Target] = ["kvstore", "redis"] if options.redis_server else ["kvstore"]
    rows = []
    for target in targets:
        for depth in (1, 16):
            with deploy(target, "everysec", options) as endpoint:
                requests = 100_000 if depth == 1 else 1_000_000
                argv = [
                    options.redis_benchmark,
                    "-h", endpoint.host,
                    "-p", str(endpoint.resp_port),
                    "-t", "set,get",
                    "-n", str(requests if target == "redis" else requests // 4),
                    "-c", str(options.clients),
                    "-r", str(options.workload.keyspace),
                    "-d", str(options.workload.value_size),
                    "-P", str(depth),
                    "--csv",
                ]  # fmt: skip
                print(f"[redis-benchmark] {target} -P {depth}", flush=True)
                out = subprocess.run(argv, capture_output=True, text=True, check=True).stdout
                for record in csv.DictReader(io.StringIO(out)):
                    row = {
                        "target": target,
                        "pipeline": depth,
                        "test": record["test"],
                        "ops_per_sec": float(record["rps"]),
                        "p50_ms": float(record["p50_latency_ms"]),
                        "p99_ms": float(record["p99_latency_ms"]),
                        "max_ms": float(record["max_latency_ms"]),
                    }
                    print(f"  {row}", flush=True)
                    rows.append(row)
    return rows


# ---------------------------------------------------------- environment
def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def _command(*argv: str) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def environment(options: SuiteOptions) -> dict[str, Any]:
    cpu = next(
        (line.split(":", 1)[1].strip() for line in _read("/proc/cpuinfo").splitlines()
         if line.startswith("model name")),
        platform.processor(),
    )  # fmt: skip
    mem_kb = next(
        (int(line.split()[1]) for line in _read("/proc/meminfo").splitlines()
         if line.startswith("MemTotal")),
        0,
    )  # fmt: skip
    try:
        import uvloop  # type: ignore[import-not-found, unused-ignore]

        loop = f"uvloop {uvloop.__version__}"
    except ImportError:
        loop = "asyncio"
    root = Path(__file__).resolve().parent.parent
    return {
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
        "kvstore_version": kvstore.__version__,
        "git_commit": _command("git", "-C", str(root), "rev-parse", "--short", "HEAD"),
        "platform": platform.platform(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "event_loop": loop,
        "cpu": cpu,
        "logical_cpus": os.cpu_count(),
        "memory_gb": round(mem_kb / 1024 / 1024, 1),
        "load_average_at_start": _read("/proc/loadavg").split()[:3],
        "redis_version": _command(options.redis_server, "--version")
        if options.redis_server
        else None,
    }


# ------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m benchmarks.suite", description=__doc__)
    p.add_argument("--out", type=Path, required=True, help="where to write the JSON results")
    p.add_argument("--redis-server", help="path to redis-server (enables the Redis baseline)")
    p.add_argument("--redis-benchmark", help="path to redis-benchmark (enables those runs)")
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--duration", type=float, default=8.0)
    p.add_argument("--warmup", type=float, default=2.0)
    p.add_argument("--clients", type=int, default=50)
    p.add_argument("--keys", type=int, default=100_000)
    p.add_argument("--value-size", type=int, default=64)
    p.add_argument("--groups", help="comma-separated subset, e.g. 'protocol,fsync'")
    p.add_argument("--no-curve", action="store_true", help="skip the open-loop latency sweep")
    p.add_argument("--work-dir", type=Path, help="parent for data dirs (default: system temp)")
    args = p.parse_args(argv)

    options = SuiteOptions(
        clients=args.clients,
        http_clients=args.clients,
        duration_s=args.duration,
        warmup_s=args.warmup,
        reps=args.reps,
        workload=Workload(keyspace=args.keys, value_size=args.value_size),
        redis_server=args.redis_server,
        redis_benchmark=args.redis_benchmark,
        work_dir=args.work_dir,
        curve_duration_s=args.duration,
    )
    matrix = scenarios(with_redis=bool(args.redis_server))
    if args.groups:
        wanted = set(args.groups.split(","))
        matrix = [s for s in matrix if s.group in wanted]

    started = time.monotonic()
    report: dict[str, Any] = {
        "environment": environment(options),
        "options": {
            "clients": options.clients,
            "duration_s": options.duration_s,
            "warmup_s": options.warmup_s,
            "reps": options.reps,
            "keyspace": options.workload.keyspace,
            "value_size": options.workload.value_size,
        },
    }
    report["scenarios"] = run_matrix(matrix, options)
    if not args.no_curve:
        report["latency_curve"] = run_latency_curve(report["scenarios"], options)
    if args.redis_benchmark:
        report["redis_benchmark"] = run_redis_benchmark(options)
    report["elapsed_s"] = round(time.monotonic() - started, 1)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({report['elapsed_s']:.0f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
