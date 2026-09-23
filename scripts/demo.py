"""A two-minute tour: shard, replicate, kill a primary, keep serving.

    python scripts/demo.py              # ~2 minutes, six shard nodes and a router
    python scripts/demo.py --fast       # no pauses, for a recording or CI

It starts three shard groups (a primary and a replica each) behind a router,
writes through it, kills a primary outright (SIGKILL, or TerminateProcess on
Windows), and shows the cluster promote the replica and carry on with every
acknowledged write still there.
Everything runs on localhost; data lands in a temporary directory that is
removed at the end.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kvstore.core.exceptions import KVStoreError
from kvstore.protocol.client import KVClient

ROUTER_TCP, ROUTER_HTTP = 7100, 8100
SHARD_TCP, SHARD_HTTP = 6500, 8500  # primaries: +0..2, replicas: +3..5
GROUPS = 3

BOLD, DIM, OFF = "\033[1m", "\033[2m", "\033[0m"
GREEN, RED, BLUE = "\033[32m", "\033[31m", "\033[36m"


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as response:
        text: str = response.read().decode()
    return text


class Demo:
    def __init__(self, pace: float, writes: int = 5000) -> None:
        self.pace = pace
        self.writes = writes
        self.dir = Path(tempfile.mkdtemp(prefix="kvstore-demo-"))
        self.nodes: dict[str, subprocess.Popen[bytes]] = {}

    # ----------------------------------------------------------- narration
    def say(self, text: str) -> None:
        print(f"\n{BOLD}{text}{OFF}", flush=True)
        time.sleep(self.pace)

    def note(self, text: str) -> None:
        print(f"{DIM}  {text}{OFF}", flush=True)

    def shown(self, label: str, value: object, good: bool | None = None) -> None:
        color = "" if good is None else (GREEN if good else RED)
        print(f"  {label:<44} {color}{value}{OFF}", flush=True)

    # ------------------------------------------------------------- cluster
    def start(self, name: str, port: int, http: int, **env: str) -> None:
        settings = {
            **os.environ,
            "KV_NODE_ID": name,
            "KV_TCP_PORT": str(port),
            "KV_HTTP_PORT": str(http),
            "KV_DATA_DIR": str(self.dir / name),
            "KV_LOG_LEVEL": "ERROR",
            "KV_ACCESS_LOG": "false",
            **env,
        }
        log = (self.dir / f"{name}.log").open("wb")
        self.nodes[name] = subprocess.Popen(
            [sys.executable, "-m", "kvstore"], env=settings, stdout=log, stderr=subprocess.STDOUT
        )

    def start_cluster(self) -> None:
        spec = []
        for i in range(GROUPS):
            primary, replica = f"g{i + 1}-primary", f"g{i + 1}-replica"
            self.start(primary, SHARD_TCP + i, SHARD_HTTP + i)
            self.start(
                replica,
                SHARD_TCP + GROUPS + i,
                SHARD_HTTP + GROUPS + i,
                KV_REPLICAOF=f"127.0.0.1:{SHARD_TCP + i}",
            )
            spec.append(f"g{i + 1}=127.0.0.1:{SHARD_TCP + i}+127.0.0.1:{SHARD_TCP + GROUPS + i}")
        self.start(
            "router",
            ROUTER_TCP,
            ROUTER_HTTP,
            KV_NODE_ROLE="router",
            KV_SHARDS=",".join(spec),
            KV_HEARTBEAT_INTERVAL_S="0.5",
            KV_DEAD_AFTER_S="2.0",
        )
        for port in [ROUTER_HTTP, *(SHARD_HTTP + i for i in range(GROUPS * 2))]:
            self.wait_http(f"http://127.0.0.1:{port}/health")

    @staticmethod
    def wait_http(url: str, seconds: float = 30.0) -> Any:
        deadline = time.monotonic() + seconds
        while True:
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    return json.loads(response.read())
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)

    @staticmethod
    def get(path: str) -> Any:
        return Demo.wait_http(f"http://127.0.0.1:{ROUTER_HTTP}{path}", seconds=5)

    def stop(self) -> None:
        for process in self.nodes.values():
            if process.poll() is None:
                process.terminate()
        for process in self.nodes.values():
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=10)
        shutil.rmtree(self.dir, ignore_errors=True)

    def client(self) -> KVClient:
        return KVClient("127.0.0.1", ROUTER_TCP, timeout_s=5)

    def two_keys_in_different_groups(self) -> tuple[str, str]:
        """Two keys the ring sends to different groups, so one command can't have both."""
        owners: dict[str, str] = {}
        for i in range(100):
            key = f"key:{i}"
            group = str(self.get(f"/v1/cluster/keys/{key}/owner")["shard"])
            if owners and group not in owners:
                return next(iter(owners.values())), key
            owners[group] = key
        raise RuntimeError("every key landed in one group")


async def run(demo: Demo) -> None:
    demo.say("1. Three shard groups, each a primary and a replica, behind one router")
    demo.start_cluster()
    for node in demo.get("/v1/cluster/nodes")["nodes"]:
        demo.shown(f"{node['shard']}  {node['address']}", f"{node['role']}  {node['state']}", True)

    demo.say("2. Writes go through the router, which hashes each key to its group")
    async with demo.client() as client:
        for key, value in (("user:1", "tejas"), ("user:2", "amol"), ("session:9", "live")):
            await client.execute("SET", key, value)
            owner = demo.get(f"/v1/cluster/keys/{key}/owner")
            demo.shown(f"SET {key} {value}", f"-> {owner['shard']} ({owner['primary']})")
        await client.execute("ZADD", "leaderboard", "120", "tejas", "95", "amol")
        members = await client.execute("ZCARD", "leaderboard")
        demo.shown("ZADD leaderboard 120 tejas 95 amol", f"{members} members")

        demo.say("3. Keys of one command must share a group -- hash tags keep them together")
        await client.execute("MSET", "{cart:9}:items", "3", "{cart:9}:total", "42")
        demo.shown("MSET {cart:9}:items 3 {cart:9}:total 42", "OK", True)
        here, there = demo.two_keys_in_different_groups()
        try:
            await client.execute("MSET", here, "1", there, "2")
        except KVStoreError as exc:
            demo.shown(f"MSET {here} 1 {there} 2", exc.message, False)

    demo.say(f"4. {demo.writes:,} writes through the router, then kill a primary outright")
    acknowledged: dict[str, str] = {}
    async with demo.client() as client:
        for start in range(0, demo.writes, 500):
            stop = min(start + 500, demo.writes)
            await client.pipeline([["SET", f"row:{i}", str(i)] for i in range(start, stop)])
            acknowledged.update({f"row:{i}": str(i) for i in range(start, stop)})
    demo.shown("acknowledged writes", f"{len(acknowledged):,}", True)

    victim = "g1-primary"
    demo.nodes[victim].kill()  # SIGKILL on POSIX, TerminateProcess on Windows
    demo.nodes[victim].wait()
    killed = time.monotonic()
    how = "TerminateProcess" if os.name == "nt" else "kill -9"
    demo.shown(f"{how} {victim}", "gone", False)

    demo.say("5. The cluster manager notices, promotes the replica, and routing follows")
    event = None
    while time.monotonic() - killed < 15:
        events = demo.get("/v1/cluster/events")
        if events:
            event = events[0]
            break
        await asyncio.sleep(0.1)
    assert event is not None, "no failover within 15 s"
    demo.shown("failover", f"{event['shard']}: {event['old_primary']} -> {event['new_primary']}")
    demo.shown("promoted after", f"{time.monotonic() - killed:.2f} s", True)
    demo.shown("epoch", event["epoch"])

    demo.say("6. Writes carry on, and every acknowledged write is still there")
    async with demo.client() as client:
        await client.execute("SET", "after:failover", "ok")
        demo.shown("SET after:failover ok", await client.execute("GET", "after:failover"), True)
        missing = 0
        keys = list(acknowledged)
        for start in range(0, len(keys), 1000):
            chunk = keys[start : start + 1000]
            values = await client.pipeline([["GET", k] for k in chunk])
            missing += sum(1 for k, v in zip(chunk, values, strict=True) if v != acknowledged[k])
        demo.shown("acknowledged writes lost", missing, missing == 0)
        top = await client.execute("ZRANGE", "leaderboard", "0", "0", "REV")
        demo.shown("leaderboard top, from the promoted replica", top, True)

    demo.say("7. Every node exposes Prometheus metrics; the router counts the failover")
    metrics = await asyncio.to_thread(fetch, f"http://127.0.0.1:{ROUTER_HTTP}/metrics")
    for line in metrics.splitlines():
        if line.startswith(("kvstore_cluster_failovers_total", "kvstore_cluster_epoch")):
            demo.note(line)
    demo.note("docker compose up --build  ->  the same cluster with Grafana on :3000")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/demo.py", description=__doc__)
    parser.add_argument("--fast", action="store_true", help="no pauses (for a recording)")
    parser.add_argument("--writes", type=int, default=5000, help="writes before the kill")
    args = parser.parse_args(argv)
    demo = Demo(pace=0.0 if args.fast else 1.2, writes=args.writes)
    try:
        asyncio.run(run(demo))
        print(f"\n{GREEN}{BOLD}The group survived losing its primary.{OFF} "
              f"{BLUE}docs/BENCHMARKS.md has the numbers.{OFF}\n")  # fmt: skip
    finally:
        demo.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
