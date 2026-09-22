"""Recovery time against log size: replaying the AOF against loading a snapshot.

    python -m benchmarks.recovery --redis-server ~/redis/src/redis-server \\
        --out benchmarks/results/phase5/recovery.json

For each size, a node is filled with SETs (100-byte values) through
pipelined RESP, stopped cleanly and restarted, three times:

1. **AOF only** -- every write is replayed from the log;
2. **snapshot** -- after ``BGREWRITEAOF`` the same keyspace is one snapshot
   file and an empty AOF;
3. **overwrites** -- the same number of writes over a tenth as many keys:
   the log grows with writes, a snapshot only with keys.

Reported: the files' size, the node's own load time (``load_duration_ms``:
reading the files and rebuilding the keyspace), and spawn-to-ready (process
start, imports, load, listeners -- what a restart costs a client; polled
every 0.1 s). With ``--redis-server``, Redis 7 does the same (its AOF, and
its RDB-preamble base after a rewrite); its load time is the one it logs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from benchmarks.servers import Deployment, Endpoint
from benchmarks.stallwatch import StallWatch
from kvstore.protocol.client import KVClient

SIZES = (125_000, 250_000, 500_000, 1_000_000)
VALUE = "v" * 100
RESTARTS = 3


async def _fill(endpoint: Endpoint, writes: int, keys: int) -> None:
    async with KVClient(endpoint.host, endpoint.resp_port, timeout_s=60) as client:
        for start in range(0, writes, 2000):
            stop = min(writes, start + 2000)
            await client.pipeline([["SET", f"key:{i % keys}", VALUE] for i in range(start, stop)])


async def _info(endpoint: Endpoint, section: str) -> dict[str, str]:
    async with KVClient(endpoint.host, endpoint.resp_port, timeout_s=10) as client:
        text: str = await client.execute("INFO", section)
    return dict(
        line.split(":", 1) for line in text.splitlines() if ":" in line and not line.startswith("#")
    )


def _admin(endpoint: Endpoint) -> dict[str, Any]:
    url = f"http://{endpoint.host}:{endpoint.http_port}/v1/admin/info"
    with urllib.request.urlopen(url, timeout=10) as response:
        persistence: dict[str, Any] = json.loads(response.read())["engine"]["persistence"]
    return persistence


async def _rewrite(endpoint: Endpoint) -> float:
    """Start a rewrite and wait for it; returns how long the command took to answer (ms).

    It should answer at once -- the work is in the background -- so a slow
    reply means the event loop was blocked, and every client with it.
    """
    async with KVClient(endpoint.host, endpoint.resp_port, timeout_s=300) as client:
        started = time.perf_counter()
        await client.execute("BGREWRITEAOF")
        reply_ms = (time.perf_counter() - started) * 1000
    await asyncio.sleep(0.5)
    while True:
        info = await _info(endpoint, "persistence")
        scheduled = info.get("aof_rewrite_scheduled", "0")  # Redis only
        if info["aof_rewrite_in_progress"] == "0" and scheduled == "0":
            return round(reply_ms, 1)
        await asyncio.sleep(0.2)


def _restarts(
    deployment: Deployment, name: str, probe: Callable[[], dict[str, Any]]
) -> dict[str, Any]:
    ready_s, probes, stalls = [], [], []
    for _ in range(RESTARTS):
        deployment.stop(name)
        with StallWatch() as watch:
            started = time.perf_counter()
            deployment.restart(name)
            ready_s.append(time.perf_counter() - started)
        stalls.append(watch.summary())
        probes.append(probe())
    return {
        "spawn_to_ready_s": round(statistics.median(ready_s), 3),
        "host_stalls": stalls,
        "runs": probes,
    }


def kvstore_case(
    work_dir: Path | None, writes: int, keys: int, *, snapshot: bool
) -> dict[str, Any]:
    with Deployment(work_dir) as deployment:
        node = deployment.kvstore_node("node", env={"KV_AOF_REWRITE_PERCENTAGE": "0"})
        asyncio.run(_fill(node, writes, keys))
        reply_ms = asyncio.run(_rewrite(node)) if snapshot else None

        def probe() -> dict[str, Any]:
            p = _admin(node)
            return {
                "load_ms": p["load_duration_ms"],
                "aof_records_loaded": p["aof_records_loaded"],
                "snapshot_keys_loaded": p["snapshot_keys_loaded"],
                "aof_bytes": p["aof_current_size"],
                "snapshot_bytes": p["aof_base_size"],
            }

        result = _restarts(deployment, "node", probe)
    runs = result.pop("runs")
    return {
        **runs[-1],
        "load_ms": round(statistics.median(r["load_ms"] for r in runs), 1),
        "bgrewriteaof_reply_ms": reply_ms,
        **result,
    }


_REDIS_LOADED = re.compile(
    r"DB loaded from (append only file|base file|incr file)[^:]*: ([0-9.]+) seconds"
)


def _redis_load_ms(log: str) -> float | None:
    """Redis's own load time, from the log lines of its last start."""
    start = log.rfind("Server initialized")
    matches = _REDIS_LOADED.findall(log[start:] if start >= 0 else log)
    total = [float(s) for kind, s in matches if kind == "append only file"]
    if total:
        return round(total[-1] * 1000, 1)
    parts = [float(s) for _, s in matches]  # the base and incr files, if no total line
    return round(sum(parts) * 1000, 1) if parts else None


def redis_case(
    work_dir: Path | None, redis_server: str, writes: int, keys: int, *, snapshot: bool
) -> dict[str, Any]:
    with Deployment(work_dir) as deployment:
        node = deployment.redis(redis_server, loglevel="notice")  # notice logs the load time
        asyncio.run(_fill(node, writes, keys))
        reply_ms = asyncio.run(_rewrite(node)) if snapshot else None

        def probe() -> dict[str, Any]:
            info = asyncio.run(_info(node, "persistence"))
            keyspace = asyncio.run(_info(node, "keyspace"))
            log = (deployment.dir / "redis.log").read_text(encoding="utf-8", errors="replace")
            return {
                "load_ms": _redis_load_ms(log),
                "aof_bytes": int(info["aof_current_size"]) - int(info["aof_base_size"]),
                "base_bytes": int(info["aof_base_size"]),
                "keys": int(keyspace.get("db0", "keys=0").split(",")[0].split("=")[1]),
            }

        result = _restarts(deployment, "redis", probe)
    runs = result.pop("runs")
    loads = [r["load_ms"] for r in runs if r["load_ms"] is not None]
    load_ms = round(statistics.median(loads), 1) if loads else None
    return {**runs[-1], "load_ms": load_ms, "bgrewriteaof_reply_ms": reply_ms, **result}


def run(
    sizes: tuple[int, ...], work_dir: Path | None, redis_server: str | None
) -> list[dict[str, Any]]:
    rows = []
    cases = [("aof", 1, False), ("snapshot", 1, True), ("overwrites_aof", 10, False),
             ("overwrites_snapshot", 10, True)]  # fmt: skip
    for writes in sizes:
        for case, overwrite, snapshot in cases:
            keys = writes // overwrite
            row: dict[str, Any] = {"writes": writes, "keys": keys, "case": case}
            row["kvstore"] = kvstore_case(work_dir, writes, keys, snapshot=snapshot)
            if redis_server:
                row["redis"] = redis_case(work_dir, redis_server, writes, keys, snapshot=snapshot)
            print(json.dumps(row), flush=True)
            rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m benchmarks.recovery", description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=list(SIZES))
    parser.add_argument("--redis-server")
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    report = {"value_bytes": len(VALUE), "restarts": RESTARTS,
              "results": run(tuple(args.sizes), args.work_dir, args.redis_server)}  # fmt: skip
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
