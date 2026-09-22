"""Chaos runs against real node processes: a full disk, and a network partition.

    python -m benchmarks.chaos --out benchmarks/results/phase5/chaos.json    # Linux / WSL

Every node is its own process; faults come from the operating system and
from :class:`~benchmarks.faultproxy.FaultProxy`, not from mocks.

* **disk_full** -- shard ``a`` runs under ``RLIMIT_FSIZE``, so once its AOF
  reaches the limit the kernel refuses the write (EFBIG, handled like
  ENOSPC). Writes go on through the router: does ``a`` refuse them cleanly
  (MISCONF), keep serving reads, and leave ``b`` alone? Then ``a`` is
  killed (SIGKILL) and restarted without the limit: is every acknowledged
  write back?
* **partition** -- the primary of ``a`` is cut off from the router, the
  cluster manager and its replica, while a client that still reaches it
  keeps writing (the minority side). How many writes does the minority
  side accept -- all of them lost -- with and without
  ``min-replicas-to-write``? Is any write the majority side acknowledged
  lost? Does exactly one primary remain after the partition heals?
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from benchmarks.faultproxy import FaultProxy
from benchmarks.servers import Deployment, Endpoint
from kvstore.cluster.hash_ring import ConsistentHashRing
from kvstore.core.exceptions import KVStoreError
from kvstore.protocol.client import KVClient


def _http(endpoint: Endpoint, path: str, body: dict[str, Any] | None = None) -> Any:
    request = urllib.request.Request(
        f"http://{endpoint.host}:{endpoint.http_port}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def _info(endpoint: Endpoint) -> str:
    result: str = _http(endpoint, "/v1/commands", {"command": "INFO", "args": ["replication"]})[
        "result"
    ]
    return result


async def _read_back(router: Endpoint, expected: dict[str, str]) -> list[str]:
    """Keys whose acknowledged value is gone."""
    keys = list(expected)
    values: list[Any] = []
    async with KVClient(router.host, router.resp_port, timeout_s=10) as client:
        for i in range(0, len(keys), 1000):
            values += await client.pipeline([["GET", k] for k in keys[i : i + 1000]])
    return [k for k, v in zip(keys, values, strict=True) if v != expected[k]]


# -------------------------------------------------------------- disk full
async def disk_full(work_dir: Path | None, limit_bytes: int = 256 * 1024) -> dict[str, Any]:
    with Deployment(work_dir) as deployment:
        a = await asyncio.to_thread(deployment.kvstore_node, "a", file_size_limit=limit_bytes)
        b = await asyncio.to_thread(deployment.kvstore_node, "b")
        shards = f"a=127.0.0.1:{a.resp_port},b=127.0.0.1:{b.resp_port}"
        router = await asyncio.to_thread(
            deployment.kvstore_node, "router", env={"KV_NODE_ROLE": "router", "KV_SHARDS": shards}
        )
        ring = ConsistentHashRing(["a", "b"])
        acked: dict[str, str] = {}
        refused_a, ok_b_after_full, errors_b = 0, 0, 0
        first_refusal: float | None = None
        refusals: dict[str, int] = {}
        started = time.monotonic()
        async with KVClient(router.host, router.resp_port, timeout_s=5) as client:
            for i in itertools.count():
                if refused_a >= 200 or time.monotonic() - started > 60:
                    break
                key, value = f"k{i}", f"value-{i}-" + "x" * 100
                group = ring.get_node(key)
                try:
                    await client.execute("SET", key, value)
                    acked[key] = value
                    if group == "b" and first_refusal is not None:
                        ok_b_after_full += 1
                except KVStoreError as exc:
                    if group == "a":
                        refused_a += 1
                        refusals[exc.prefix] = refusals.get(exc.prefix, 0) + 1
                        if first_refusal is None:
                            first_refusal = time.monotonic()
                    else:
                        errors_b += 1
            in_a = [k for k in acked if ring.get_node(k) == "a"]
            reads = await client.pipeline([["GET", k] for k in in_a[-100:]])
            reads_served = sum(1 for k, v in zip(in_a[-100:], reads, strict=True) if v == acked[k])
        events = await asyncio.to_thread(_http, router, "/v1/cluster/events")

        # Crash it, give it back its disk, and check what survived.
        deployment.kill("a")
        await asyncio.to_thread(deployment.restart, "a")
        info = await asyncio.to_thread(_http, a, "/v1/admin/info")
        lost = await _read_back(router, acked)
    persistence = info["engine"]["persistence"]
    return {
        "limit_bytes": limit_bytes,
        "acked_writes": len(acked),
        "acked_in_full_shard": len(in_a),
        "first_refusal_after_s": round(first_refusal - started, 3) if first_refusal else None,
        # CLUSTERDOWN: the batch whose commit failed (its connection is dropped,
        # so no reply claims success); MISCONF: every write after that.
        "refusals": refusals,
        "refused_writes": refused_a,
        "reads_served_while_full": f"{reads_served}/{min(100, len(in_a))}",
        "other_shard_writes_after_full": ok_b_after_full,
        "other_shard_errors": errors_b,
        "failovers": len(events),
        "after_restart": {
            "aof_records_loaded": persistence["aof_records_loaded"],
            "aof_truncated_bytes": persistence["aof_truncated_bytes"],
            "acked_writes_lost": len(lost),
        },
    }


# -------------------------------------------------------------- partition
async def partition(
    work_dir: Path | None, *, min_replicas: int, partition_s: float = 6.0
) -> dict[str, Any]:
    with Deployment(work_dir) as deployment:
        p = await asyncio.to_thread(
            deployment.kvstore_node,
            "p",
            env={
                "KV_MIN_REPLICAS_TO_WRITE": str(min_replicas),
                "KV_MIN_REPLICAS_MAX_LAG_S": "2.5",
            },
        )
        # The primary's address for everyone -- router, manager and replica --
        # is the proxy's, so one cut isolates it. (One address, as configured:
        # with two, the manager would re-point the replica at the other.)
        proxy = FaultProxy(p.host, p.resp_port)
        await proxy.start()
        try:
            r = await asyncio.to_thread(
                deployment.kvstore_node, "r", env={"KV_REPLICAOF": proxy.address}
            )
            b = await asyncio.to_thread(deployment.kvstore_node, "b")
            shards = f"a={proxy.address}+127.0.0.1:{r.resp_port},b=127.0.0.1:{b.resp_port}"
            router = await asyncio.to_thread(
                deployment.kvstore_node,
                "router",
                env={
                    "KV_NODE_ROLE": "router",
                    "KV_SHARDS": shards,
                    "KV_HEARTBEAT_INTERVAL_S": "0.2",
                    "KV_SUSPECT_AFTER_S": "0.5",
                    "KV_DEAD_AFTER_S": "1.0",
                },
            )
            result = await _partition_run(router, p, proxy, partition_s)
        finally:
            await proxy.stop()
    return {"min_replicas_to_write": min_replicas, **result}


async def _partition_run(
    router: Endpoint,
    p: Endpoint,
    proxy: FaultProxy,
    partition_s: float,
) -> dict[str, Any]:
    ring = ConsistentHashRing(["a", "b"])
    majority: dict[str, str] = {}
    majority_times: dict[str, list[float]] = {"a": [], "b": []}
    failures: dict[str, int] = {"a": 0, "b": 0}
    minority: list[tuple[str, float]] = []  # (key, acknowledged at)
    minority_errors: dict[str, int] = {}
    stop = asyncio.Event()

    async def majority_writer(n: int) -> None:
        async with KVClient(router.host, router.resp_port, timeout_s=1) as client:
            for i in itertools.count():
                if stop.is_set():
                    return
                key = f"w{n}:{i}"
                try:
                    if await client.execute("SET", key, str(i)) == "OK":
                        majority[key] = str(i)
                        majority_times[ring.get_node(key)].append(time.monotonic())
                except KVStoreError:
                    failures[ring.get_node(key)] += 1
                    await asyncio.sleep(0.01)

    async def minority_writer() -> None:
        # Straight to the old primary: the side of the partition that loses.
        async with KVClient(p.host, p.resp_port, timeout_s=1) as client:
            for i in itertools.count():
                if stop.is_set():
                    return
                try:
                    if await client.execute("SET", f"minority:{i}", "x") == "OK":
                        minority.append((f"minority:{i}", time.monotonic()))
                except KVStoreError as exc:
                    minority_errors[exc.prefix] = minority_errors.get(exc.prefix, 0) + 1
                await asyncio.sleep(0.005)

    tasks = [asyncio.create_task(majority_writer(n)) for n in range(8)]
    await asyncio.sleep(2.0)
    minority_task = asyncio.create_task(minority_writer())
    await asyncio.sleep(0.5)
    proxy.partition()
    cut_at, cut_wall = time.monotonic(), time.time()
    await asyncio.sleep(partition_s)
    proxy.heal()
    healed_at = time.monotonic()
    await asyncio.sleep(3.0)
    stop.set()
    await asyncio.gather(*tasks, minority_task)

    rejoined_after: float | None = None
    for _ in range(50):
        if "role:slave" in await asyncio.to_thread(_info, p):
            rejoined_after = round(time.monotonic() - healed_at, 3)
            break
        await asyncio.sleep(0.2)
    events = await asyncio.to_thread(_http, router, "/v1/cluster/events")
    config = await asyncio.to_thread(_http, router, "/v1/cluster/config")
    lost = await _read_back(router, majority)
    # Only writes acknowledged during the partition: earlier ones replicated normally.
    during = {key: at for key, at in minority if at > cut_at}
    gone = await _read_back(router, dict.fromkeys(during, "x"))
    gaps = [b - a for a, b in itertools.pairwise(sorted(majority_times["a"])) if b > cut_at - 1]
    event = events[0] if events else None
    return {
        "partition_s": partition_s,
        "promotion_after_s": round(event["promoted_at"] - cut_wall, 3) if event else None,
        "majority_outage_s": round(max(gaps), 3) if gaps else None,
        "majority_acked_writes": len(majority),
        "majority_acked_writes_lost": len(lost),
        "failures_other_group": failures["b"],
        "minority_writes_accepted_during_partition": len(during),
        "minority_accepted_for_s": round(max(during.values()) - cut_at, 3) if during else 0.0,
        "minority_refusals": minority_errors,
        "minority_writes_surviving": len(during) - len(gone),
        "old_primary_rejoined_as_replica_after_s": rejoined_after,
        "primaries_after": [g["primary"] for g in config["shards"]],
        "epoch_after": config["epoch"],
    }


# -------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m benchmarks.chaos", description=__doc__)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args(argv)
    report: dict[str, Any] = {}
    report["disk_full"] = asyncio.run(disk_full(args.work_dir))
    print("disk_full:", json.dumps(report["disk_full"], indent=1), flush=True)
    report["partition"] = []
    for min_replicas in (0, 1):
        result = asyncio.run(partition(args.work_dir, min_replicas=min_replicas))
        report["partition"].append(result)
        print(f"partition (min_replicas={min_replicas}):", json.dumps(result, indent=1), flush=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
