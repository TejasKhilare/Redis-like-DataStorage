"""Microbenchmarks behind two Phase 5 changes: the AOF record format, and what metrics cost.

    python -m benchmarks.micro --out benchmarks/results/phase5/micro.json

* **AOF record**: one ``SET`` (16-byte key, 64-byte value) encoded as v2
  (JSON + CRC, kvstore 0.3) and as v3 (RESP + CRC); alone, and with a
  replica attached, where v2 also had to encode the RESP the replication
  stream carries (twice in all) and v3 reuses its payload (once). Plus each
  record's size, and decoding it (what recovery does per record).
* **Metrics**: recording one command -- ``CommandStats.record`` plus the
  two clock reads around the command -- against a labelled
  ``prometheus_client`` histogram (if installed); a node's whole ``SET``
  path (``ShardNode.execute``, in memory) with ``KV_METRICS_ENABLED`` on and
  off, whose difference is everything metrics add per command (best and
  median of 9 alternating runs); and a whole in-process ``SET`` logged to
  the AOF, for scale.

CPU-only loops, timed with :mod:`timeit`: the best of 7 repeats, the run
least disturbed by the rest of the machine.
"""

from __future__ import annotations

import argparse
import gc
import json
import statistics
import sys
import tempfile
import time
import timeit
import zlib
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

from kvstore.engine import Engine
from kvstore.engine.persistence.aof import decode_record, encode_record, frame_record
from kvstore.observability.metrics import CommandStats
from kvstore.protocol.resp import encode_command
from kvstore.replication.node import ShardNode

KEY, VALUE = "user:0000001234", "v" * 64
REPEATS = 7


def encode_v2(command: str, args: Sequence[Any]) -> bytes:
    """kvstore 0.3's record, as it was (git show 52bb49f^:.../aof.py)."""
    payload = json.dumps([command, *args], separators=(",", ":")).encode()
    return b"%08x %s\n" % (zlib.crc32(payload), payload)


def best_us(fn: Callable[[], object], number: int) -> float:
    """Microseconds per call: the best of REPEATS runs of ``number`` calls."""
    return min(timeit.repeat(fn, number=number, repeat=REPEATS)) / number * 1e6


def aof() -> dict[str, Any]:
    args = [KEY, VALUE]
    n = 200_000

    def v2_replicated() -> None:
        encode_v2("SET", args)
        encode_command(["SET", *args])  # the replication stream's copy

    def v3_replicated() -> None:
        frame_record(encode_command(["SET", *args]))  # one payload for both

    v2, v3 = encode_v2("SET", args), encode_record("SET", args)
    assert decode_record(v2) == decode_record(v3) == ("SET", args)
    result = {
        "v2_encode_us": best_us(lambda: encode_v2("SET", args), n),
        "v3_encode_us": best_us(lambda: encode_record("SET", args), n),
        "v2_replicated_us": best_us(v2_replicated, n),
        "v3_replicated_us": best_us(v3_replicated, n),
        "v2_decode_us": best_us(lambda: decode_record(v2), n),
        "v3_decode_us": best_us(lambda: decode_record(v3), n),
        "v2_record_bytes": len(v2),
        "v3_record_bytes": len(v3),
    }
    return {k: round(v, 3) if isinstance(v, float) else v for k, v in result.items()}


def node_set(keys: int = 100_000, reps: int = 9) -> dict[str, Any]:
    """A node's SET with metrics on and off, fairly: both keyspaces filled first (the same
    heap for both), a collection before each run, and the order alternating. Timed in a
    fixed order on a growing heap instead, the difference came out ~2.5x too large."""
    nodes = {"on": ShardNode(Engine(), metrics=True), "off": ShardNode(Engine(), metrics=False)}
    for node in nodes.values():
        for i in range(keys):
            node.execute("SET", f"key:{i}", VALUE)
    runs: dict[str, list[float]] = {"on": [], "off": []}
    for rep in range(reps):
        for mode in ("on", "off") if rep % 2 else ("off", "on"):
            counter = iter(range(10**9))

            def one(node: ShardNode = nodes[mode], counter: Iterator[int] = counter) -> None:
                node.execute("SET", f"key:{next(counter) % keys}", VALUE)

            gc.collect()
            runs[mode].append(timeit.timeit(one, number=keys) / keys * 1e6)
    result: dict[str, Any] = {}
    for mode, times in runs.items():
        result[f"node_set_metrics_{mode}_us"] = min(times)
        result[f"node_set_metrics_{mode}_median_us"] = statistics.median(times)
    result["node_metrics_cost_us"] = (
        result["node_set_metrics_on_us"] - result["node_set_metrics_off_us"]
    )
    result["node_metrics_cost_median_us"] = (
        result["node_set_metrics_on_median_us"] - result["node_set_metrics_off_median_us"]
    )
    return result


def metrics() -> dict[str, Any]:
    n = 500_000
    stats = CommandStats()

    def kvstore_record() -> None:
        started = time.perf_counter()
        stats.record("set", time.perf_counter() - started)

    def no_metrics() -> None:
        started = time.perf_counter()  # what the command path does anyway: nothing
        del started

    result: dict[str, Any] = {
        "kvstore_record_us": best_us(kvstore_record, n) - best_us(no_metrics, n),
    }
    try:
        from prometheus_client import CollectorRegistry, Histogram
    except ImportError:
        result["prometheus_client_us"] = None
    else:
        histogram = Histogram(
            "cmd_seconds", "per command", ["command"], registry=CollectorRegistry()
        )

        def prometheus_record() -> None:
            started = time.perf_counter()
            histogram.labels("set").observe(time.perf_counter() - started)

        result["prometheus_client_us"] = best_us(prometheus_record, n) - best_us(no_metrics, n)

    result.update(node_set())

    with (
        tempfile.TemporaryDirectory(prefix="kvbench-micro-") as tmp,
        Engine(data_dir=Path(tmp), aof_fsync="no", aof_rewrite_percentage=0) as engine,
    ):
        counter = iter(range(10**9))
        result["engine_set_with_aof_us"] = best_us(
            lambda: engine.execute("SET", f"key:{next(counter)}", VALUE), 50_000
        )
    return {k: round(v, 3) if isinstance(v, float) else v for k, v in result.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m benchmarks.micro", description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    report = {"aof": aof(), "metrics": metrics()}
    print(json.dumps(report, indent=1))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
