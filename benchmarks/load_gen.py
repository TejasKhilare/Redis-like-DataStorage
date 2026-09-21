"""asyncio load generator for kvstore (RESP or HTTP) and for Redis itself.

    python -m benchmarks.load_gen --port 6379                      # 50 clients, 90% GET, 10 s
    python -m benchmarks.load_gen --port 6379 --pipeline 16 --read-ratio 0
    python -m benchmarks.load_gen --protocol http --port 8000 --clients 20
    python -m benchmarks.load_gen --port 6379 --rate 5000          # open loop, fixed offered load

Two ways to drive load:

* **Closed loop** (default): every connection sends its next request as soon
  as the previous reply arrives. This finds the maximum throughput, but its
  latencies flatter the server: while a request is stuck, that connection
  stops sending, so the stall is recorded once instead of for every request
  that would have been waiting behind it ("coordinated omission").
* **Open loop** (``--rate``): requests are scheduled at a fixed rate and each
  latency is measured from when the request *should* have been sent, as wrk2
  does. A stall then shows up in every request it delayed, so these
  percentiles are the ones to quote for a given load.

A pipelined batch counts each of its commands with the batch's latency, like
redis-benchmark. ``--processes`` spreads the connections over several
processes when one Python process can't generate enough load; their
histograms are merged exactly.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import math
import multiprocessing
import sys
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import asdict, dataclass, field
from typing import Any, TypeVar

from benchmarks.drivers import Connection, Protocol, RequestEncoder
from benchmarks.histogram import LatencyHistogram
from benchmarks.workload import OperationStream, Workload

_T = TypeVar("_T")
_NS = 1_000_000_000


@dataclass(frozen=True, slots=True)
class LoadConfig:
    host: str = "127.0.0.1"
    port: int = 6379
    protocol: Protocol = "resp"
    clients: int = 50
    pipeline: int = 1
    duration_s: float = 10.0
    warmup_s: float = 2.0
    rate: float = 0.0  # total offered ops/s; 0 = closed loop
    processes: int = 1
    seed: int = 1
    workload: Workload = field(default_factory=Workload)

    def __post_init__(self) -> None:
        if self.clients < 1 or self.pipeline < 1 or self.processes < 1:
            raise ValueError("clients, pipeline and processes must be at least 1")
        if self.processes > self.clients:
            raise ValueError("processes cannot exceed clients")
        if self.duration_s <= 0 or self.warmup_s < 0 or self.rate < 0:
            raise ValueError("duration must be positive; warmup and rate non-negative")
        if self.protocol == "http" and self.pipeline > 1:
            raise ValueError("HTTP/1.1 pipelining is not supported; use --pipeline 1")
        if self.rate and self.pipeline > 1:
            raise ValueError("open-loop mode (--rate) sends one request at a time")

    @property
    def mode(self) -> str:
        return "open" if self.rate else "closed"


@dataclass(slots=True)
class LoadResult:
    config: LoadConfig
    ops: int
    errors: int
    duration_s: float
    histogram: LatencyHistogram
    timeline: list[int]  # completed ops per second of the measured window

    @property
    def ops_per_sec(self) -> float:
        return self.ops / self.duration_s if self.duration_s else 0.0

    def to_dict(self, *, include_histogram: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "config": asdict(self.config),
            "ops": self.ops,
            "errors": self.errors,
            "duration_s": round(self.duration_s, 3),
            "ops_per_sec": round(self.ops_per_sec, 1),
            "latency_us": self.histogram.summary_us(),
            "timeline": self.timeline,
        }
        if include_histogram:
            data["histogram"] = self.histogram.to_dict()
        return data


# ---------------------------------------------------------------- worker
@dataclass(slots=True)
class _Tally:
    histogram: LatencyHistogram = field(default_factory=LatencyHistogram)
    ops: int = 0
    errors: int = 0
    timeline: list[int] = field(default_factory=list)

    def add(
        self, finished_ns: int, window_start: int, latency_ns: int, ops: int, errors: int
    ) -> None:
        self.histogram.record(latency_ns, ops)
        self.ops += ops
        self.errors += errors
        second = (finished_ns - window_start) // _NS
        if second < len(self.timeline):
            self.timeline[second] += ops


async def _closed_loop(
    conn: Connection,
    config: LoadConfig,
    stream: OperationStream,
    encoder: RequestEncoder,
    tally: _Tally,
    window: tuple[int, int],
) -> None:
    measure_from, stop_at = window
    depth = config.pipeline
    clock = time.perf_counter_ns
    while True:
        parts = []
        for _ in range(depth):
            is_read, key = stream.next()
            parts.append(encoder.get(key) if is_read else encoder.set(key))
        payload = b"".join(parts)
        started = clock()
        if started >= stop_at:
            return
        errors = await conn.send(payload, depth)
        finished = clock()
        if started >= measure_from:
            tally.add(finished, measure_from, finished - started, depth, errors)


async def _open_loop(
    conn: Connection,
    config: LoadConfig,
    stream: OperationStream,
    encoder: RequestEncoder,
    tally: _Tally,
    window: tuple[int, int],
    *,
    interval_ns: int,
    first_ns: int,
) -> None:
    measure_from, stop_at = window
    clock = time.perf_counter_ns
    intended = first_ns
    while intended < stop_at:
        now = clock()
        if now < intended:
            await asyncio.sleep((intended - now) / _NS)
        is_read, key = stream.next()
        errors = await conn.send(encoder.get(key) if is_read else encoder.set(key), 1)
        finished = clock()
        if intended >= measure_from:
            # Measured from the scheduled send time: time spent waiting for a
            # slow previous reply counts against this request (wrk2-style).
            tally.add(finished, measure_from, finished - intended, 1, errors)
        intended += interval_ns


async def run_load(
    config: LoadConfig, *, first_client: int = 0, clients: int | None = None, start_at: float = 0
) -> LoadResult:
    """Run ``clients`` connections (default: all) in this process's event loop.

    ``start_at`` (wall clock) lets several processes begin at the same moment.
    """
    clients = config.clients if clients is None else clients
    encoder = RequestEncoder(config.protocol, config.workload.value())
    conns = [
        await Connection.open(config.host, config.port, config.protocol) for _ in range(clients)
    ]
    try:
        delay = start_at - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        tally = _Tally(timeline=[0] * math.ceil(config.duration_s))
        began = time.perf_counter_ns()
        measure_from = began + int(config.warmup_s * _NS)
        window = (measure_from, measure_from + int(config.duration_s * _NS))
        tasks: list[Awaitable[None]] = []
        for i, conn in enumerate(conns):
            client_id = first_client + i
            stream = OperationStream(config.workload, seed=config.seed * 1_000_003 + client_id)
            if config.rate:
                interval_ns = int(_NS * config.clients / config.rate)
                # Stagger the connections evenly so they don't all fire at once.
                first_ns = began + interval_ns * client_id // config.clients
                tasks.append(
                    _open_loop(
                        conn,
                        config,
                        stream,
                        encoder,
                        tally,
                        window,
                        interval_ns=interval_ns,
                        first_ns=first_ns,
                    )
                )
            else:
                tasks.append(_closed_loop(conn, config, stream, encoder, tally, window))
        await asyncio.gather(*tasks)
        elapsed = (time.perf_counter_ns() - measure_from) / _NS
    finally:
        for conn in conns:
            conn.close()
    return LoadResult(
        config=config,
        ops=tally.ops,
        errors=tally.errors,
        duration_s=min(elapsed, config.duration_s),
        histogram=tally.histogram,
        timeline=tally.timeline,
    )


def run_event_loop(main: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
    """``asyncio.run``, on uvloop when it is installed (Linux/macOS)."""
    try:
        uvloop = importlib.import_module("uvloop")
    except ImportError:
        return asyncio.run(main())
    result: _T = uvloop.run(main())
    return result


def _worker(config: LoadConfig, first_client: int, clients: int, start_at: float) -> dict[str, Any]:
    result = run_event_loop(
        lambda: run_load(config, first_client=first_client, clients=clients, start_at=start_at)
    )
    return result.to_dict()


def run(config: LoadConfig) -> LoadResult:
    """Run the load test, fanning out to ``config.processes`` processes."""
    if config.processes == 1:
        return run_event_loop(lambda: run_load(config))
    share, extra = divmod(config.clients, config.processes)
    jobs, first = [], 0
    start_at = time.time() + 1.0 + 0.25 * config.processes  # time for workers to spawn
    for i in range(config.processes):
        count = share + (i < extra)
        jobs.append((config, first, count, start_at))
        first += count
    with multiprocessing.get_context("spawn").Pool(config.processes) as pool:
        parts = pool.starmap(_worker, jobs)
    histogram = LatencyHistogram()
    timeline = [0] * math.ceil(config.duration_s)
    for part in parts:
        histogram.merge(LatencyHistogram.from_dict(part["histogram"]))
        timeline = [a + b for a, b in zip(timeline, part["timeline"], strict=True)]
    return LoadResult(
        config=config,
        ops=sum(p["ops"] for p in parts),
        errors=sum(p["errors"] for p in parts),
        duration_s=max(p["duration_s"] for p in parts),
        histogram=histogram,
        timeline=timeline,
    )


# ---------------------------------------------------------------- preload
async def preload(host: str, port: int, workload: Workload, *, batch: int = 500) -> None:
    """SET every key of the keyspace (over RESP, pipelined) so that GETs hit."""
    encoder = RequestEncoder("resp", workload.value())
    conn = await Connection.open(host, port, "resp")
    try:
        for start in range(0, workload.keyspace, batch):
            end = min(start + batch, workload.keyspace)
            payload = b"".join(encoder.set(workload.key(i)) for i in range(start, end))
            if await conn.send(payload, end - start):
                raise RuntimeError(f"preload failed: the server rejected SETs at {host}:{port}")
    finally:
        conn.close()


# --------------------------------------------------------------------- CLI
def format_result(result: LoadResult) -> str:
    c, w = result.config, result.config.workload
    lat = result.histogram.summary_us()
    mode = f"open loop at {c.rate:,.0f} ops/s" if c.rate else "closed loop"
    reads = round(w.read_ratio * 100)
    return "\n".join(
        [
            f"{c.protocol}://{c.host}:{c.port}  {c.clients} clients x pipeline {c.pipeline}, "
            f"{mode}, {reads}% GET / {100 - reads}% SET, "
            f"{w.keyspace:,} keys ({w.distribution}), {w.value_size} B values",
            f"  throughput  {result.ops_per_sec:>12,.0f} ops/s   "
            f"({result.ops:,} ops in {result.duration_s:.1f} s, {result.errors:,} errors)",
            "  latency us  " + "  ".join(f"{name} {value:,.0f}" for name, value in lat.items()),
        ]
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m benchmarks.load_gen",
        description="Load generator for kvstore and Redis (throughput and tail latency).",
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=6379)
    p.add_argument("--protocol", choices=["resp", "http"], default="resp")
    p.add_argument("-c", "--clients", type=int, default=50, help="connections (default 50)")
    p.add_argument("-P", "--pipeline", type=int, default=1, help="commands per batch")
    p.add_argument("-d", "--duration", type=float, default=10.0, help="measured seconds")
    p.add_argument("--warmup", type=float, default=2.0, help="unmeasured seconds first")
    p.add_argument("--rate", type=float, default=0.0, help="open loop: total offered ops/s")
    p.add_argument("--processes", type=int, default=1, help="load-generator processes")
    p.add_argument("--read-ratio", type=float, default=0.9, help="fraction of GETs (0..1)")
    p.add_argument("--keys", type=int, default=100_000, help="keyspace size")
    p.add_argument("--value-size", type=int, default=64, help="SET value bytes")
    p.add_argument("--distribution", choices=["uniform", "zipf"], default="uniform")
    p.add_argument("--no-preload", action="store_true", help="skip filling the keyspace first")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--json", metavar="PATH", help="also write the result as JSON ('-' = stdout)")
    return p


def config_from_args(args: argparse.Namespace) -> LoadConfig:
    workload = Workload(
        read_ratio=args.read_ratio,
        keyspace=args.keys,
        value_size=args.value_size,
        distribution=args.distribution,
    )
    return LoadConfig(
        host=args.host,
        port=args.port,
        protocol=args.protocol,
        clients=args.clients,
        pipeline=args.pipeline,
        duration_s=args.duration,
        warmup_s=args.warmup,
        rate=args.rate,
        processes=args.processes,
        seed=args.seed,
        workload=workload,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = config_from_args(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not args.no_preload and config.workload.read_ratio > 0:
        resp_port = config.port if config.protocol == "resp" else None
        if resp_port is None:
            print("note: HTTP target, skipping preload (fill the keyspace over RESP first)")
        else:
            run_event_loop(lambda: preload(config.host, resp_port, config.workload))
    result = run(config)
    print(format_result(result))
    if args.json:
        text = json.dumps(result.to_dict(), indent=2)
        if args.json == "-":
            print(text)
        else:
            with open(args.json, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
