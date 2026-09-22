"""Kill a primary under load; measure the failover and what it lost.

    python -m benchmarks.failover                        # asynchronous replication
    python -m benchmarks.failover --wait-replicas 1      # a write is acked once a replica has it
    python -m benchmarks.failover --runs 5 --out benchmarks/results/failover.json

A cluster of shard groups (each a primary and a replica, every node its own
process) runs behind a router with its cluster manager. Writer clients
write unique keys through the router the whole time, so every acknowledged
write can be checked afterwards. Mid-run one primary is killed with SIGKILL
and later restarted on the same address and data directory.

The kill comes at a random point of the heartbeat cycle (a uniform delay
of up to one interval, recorded per run). Detection takes the dead-after
timeout minus the time since the last heartbeat, so a kill at a fixed time
after start-up hits the same point of the cycle every run and the median
reflects that one alignment. Before this was added, runs clustered: in
Phase 4, four of five at 1.53-1.57 s in one mode and four of five at
1.98-2.05 s in the other, with the same code.

Reported per run:

* **promotion**: kill -> the manager promoted the replica (detection + promotion);
* **outage**: the longest stretch without an acknowledged write to that
  group around the kill, as its clients saw it (writes to the other groups
  are counted separately);
* **lost**: acknowledged writes missing afterwards (asynchronous replication
  loses what the replica had not received yet);
* **rejoin**: the old primary came back as a replica of the new one.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import random
import statistics
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from benchmarks.servers import Deployment, Endpoint
from kvstore.cluster.hash_ring import ConsistentHashRing
from kvstore.core.exceptions import KVStoreError
from kvstore.protocol.client import KVClient


@dataclass(frozen=True, slots=True)
class FailoverConfig:
    groups: int = 3
    writers: int = 20
    before_s: float = 4.0  # load before the kill
    after_s: float = 10.0  # load after it
    restart_after_s: float = 4.0  # the killed primary comes back after this
    wait_replicas: int = 0
    heartbeat_interval_s: float = 0.5
    suspect_after_s: float = 1.0
    dead_after_s: float = 2.0
    fsync: str = "everysec"
    work_dir: Path | None = None


@dataclass(slots=True)
class _Log:
    acked: dict[str, tuple[str, float]] = field(default_factory=dict)  # key -> (value, when)
    failed: list[tuple[str, float]] = field(default_factory=list)  # (group, when)


async def _writer(
    index: int, router: Endpoint, ring: ConsistentHashRing, log: _Log, stop: asyncio.Event
) -> None:
    async with KVClient(router.host, router.resp_port, timeout_s=1.0) as client:
        seq = 0
        while not stop.is_set():
            key, value = f"w{index}:{seq}", str(seq)
            seq += 1
            try:
                ok = await client.execute("SET", key, value) == "OK"
            except KVStoreError:
                ok = False
            now = time.monotonic()
            if ok:
                log.acked[key] = (value, now)
            else:
                log.failed.append((ring.get_node(key), now))
                await asyncio.sleep(0.01)


def _http(endpoint: Endpoint, path: str, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"http://{endpoint.host}:{endpoint.http_port}{path}",
        data=data,
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def _role(endpoint: Endpoint) -> str:
    info = _http(endpoint, "/v1/commands", {"command": "INFO", "args": ["replication"]})["result"]
    return "replica" if "role:slave" in info and "master_link_status:up" in info else "other"


async def run_once(config: FailoverConfig) -> dict[str, Any]:
    router_env = {
        "KV_HEARTBEAT_INTERVAL_S": str(config.heartbeat_interval_s),
        "KV_SUSPECT_AFTER_S": str(config.suspect_after_s),
        "KV_DEAD_AFTER_S": str(config.dead_after_s),
        "KV_WAIT_REPLICAS": str(config.wait_replicas),
    }
    with Deployment(config.work_dir) as deployment:
        router, members = await asyncio.to_thread(
            deployment.replicated_cluster, config.groups, fsync=config.fsync, **router_env
        )
        ring = ConsistentHashRing([f"g{i}" for i in range(1, config.groups + 1)])
        victim = "g1"
        victim_primary = members[victim][0]
        log, stop = _Log(), asyncio.Event()
        writers = [
            asyncio.create_task(_writer(i, router, ring, log, stop)) for i in range(config.writers)
        ]
        started = time.monotonic()
        jitter = random.uniform(0, config.heartbeat_interval_s)  # see the module docstring
        await asyncio.sleep(config.before_s + jitter)
        steady_ops = len(log.acked) / (time.monotonic() - started)

        deployment.kill(f"{victim}-primary")
        killed_at, killed_wall = time.monotonic(), time.time()
        await asyncio.sleep(config.restart_after_s)
        await asyncio.to_thread(deployment.restart, f"{victim}-primary")
        await asyncio.sleep(max(0.0, config.after_s - (time.monotonic() - killed_at)))
        stop.set()
        await asyncio.gather(*writers)

        events = await asyncio.to_thread(_http, router, "/v1/cluster/events")
        rejoined = False
        for _ in range(50):  # the manager demotes it within a few heartbeats
            if await asyncio.to_thread(_role, victim_primary) == "replica":
                rejoined = True
                break
            await asyncio.sleep(0.2)

        # Every acknowledged write must still be there.
        keys = list(log.acked)
        async with KVClient(router.host, router.resp_port, timeout_s=10) as client:
            values: list[Any] = []
            for i in range(0, len(keys), 1000):
                values += await client.pipeline([["GET", k] for k in keys[i : i + 1000]])
        lost = [k for k, v in zip(keys, values, strict=True) if v != log.acked[k][0]]

    # Replies to writes the primary answered just before dying arrive after
    # `killed_at`, so the outage is the widest gap between acknowledgements.
    in_victim = sorted(t for k, (_, t) in log.acked.items() if ring.get_node(k) == victim)
    around = [t for t in in_victim if t > killed_at - 1.0]
    gaps = [(b - a, b) for a, b in itertools.pairwise(around)]
    outage, recovered_at = max(gaps) if gaps else (None, None)
    victim_failures = [t for g, t in log.failed if g == victim and t > killed_at]
    other_failures = [t for g, t in log.failed if g != victim and t > killed_at]
    event = next((e for e in events if e["shard"] == victim), None)
    return {
        "config": {k: str(v) if isinstance(v, Path) else v for k, v in asdict(config).items()},
        "steady_ops_per_sec": round(steady_ops, 1),
        "kill_jitter_s": round(jitter, 3),
        "acked_writes": len(log.acked),
        "promotion_s": round(event["promoted_at"] - killed_wall, 3) if event else None,
        "detected_after_s": event["detected_after_s"] if event else None,
        "outage_s": round(outage, 3) if outage is not None else None,
        "recovered_after_kill_s": (
            round(recovered_at - killed_at, 3) if recovered_at is not None else None
        ),
        "failed_writes_victim": len(victim_failures),
        "failed_writes_other_groups": len(other_failures),
        "lost_writes": len(lost),
        "lost_in_victim_group": sum(ring.get_node(k) == victim for k in lost),
        "lost_last_ack_before_kill_ms": (
            round((killed_at - max(log.acked[k][1] for k in lost)) * 1000, 1) if lost else None
        ),
        "old_primary_rejoined_as_replica": rejoined,
        "failover_event": event,
    }


def _summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    def med(key: str) -> float | None:
        values = [r[key] for r in runs if r[key] is not None]
        return round(statistics.median(values), 3) if values else None

    return {
        "runs": len(runs),
        "steady_ops_per_sec": med("steady_ops_per_sec"),
        "promotion_s": med("promotion_s"),
        "outage_s": med("outage_s"),
        "outage_s_max": max((r["outage_s"] or 0) for r in runs),
        "lost_writes_total": sum(r["lost_writes"] for r in runs),
        "acked_writes_total": sum(r["acked_writes"] for r in runs),
        "failed_writes_other_groups": sum(r["failed_writes_other_groups"] for r in runs),
        "rejoined": sum(r["old_primary_rejoined_as_replica"] for r in runs),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m benchmarks.failover", description=__doc__)
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--groups", type=int, default=3)
    p.add_argument("--writers", type=int, default=20)
    p.add_argument("--wait-replicas", type=int, default=[0], nargs="+", help="one or more modes")
    p.add_argument("--heartbeat", type=float, default=0.5, help="heartbeat interval (s)")
    p.add_argument("--dead-after", type=float, default=2.0, help="silence before failover (s)")
    p.add_argument("--fsync", default="everysec")
    p.add_argument("--out", type=Path)
    args = p.parse_args(argv)
    modes = args.wait_replicas if isinstance(args.wait_replicas, list) else [args.wait_replicas]
    report: dict[str, Any] = {"modes": {}}
    for mode in modes:
        config = FailoverConfig(
            groups=args.groups,
            writers=args.writers,
            wait_replicas=mode,
            heartbeat_interval_s=args.heartbeat,
            suspect_after_s=args.dead_after / 2,
            dead_after_s=args.dead_after,
            fsync=args.fsync,
        )
        runs = []
        for i in range(args.runs):
            result = asyncio.run(run_once(config))
            runs.append(result)
            print(
                f"[wait_replicas={mode} run {i + 1}] steady {result['steady_ops_per_sec']:,.0f} "
                f"ops/s, promotion {result['promotion_s']} s, outage {result['outage_s']} s, "
                f"lost {result['lost_writes']} of {result['acked_writes']:,} acked, "
                f"other groups failed {result['failed_writes_other_groups']}, "
                f"rejoined {result['old_primary_rejoined_as_replica']}",
                flush=True,
            )
        report["modes"][str(mode)] = {"summary": _summary(runs), "runs": runs}
        print(f"summary wait_replicas={mode}: {report['modes'][str(mode)]['summary']}")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
