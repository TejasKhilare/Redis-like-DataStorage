"""Chaos: network partitions (split brain), slow nodes and a full disk, in a real cluster.

Faults are injected by a proxy (``benchmarks.faultproxy``) that is the
primary of group ``a``'s address for everyone else: router and manager ->
primary, and replica -> primary (replication). One address, as in the
cluster config -- with two, the manager would re-point the replica at the
configured one, and the reconnect races the test. A client that talks to the
primary directly plays the part of the minority side of a partition.
"""

import asyncio
import errno
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from benchmarks.faultproxy import FaultProxy
from kvstore.cluster.manager import ClusterManager
from kvstore.core.exceptions import KVStoreError, NotEnoughReplicasError
from kvstore.protocol.client import KVClient
from tests.helpers import make_settings, running_app
from tests.integration.test_replication import Node, eventually, in_sync, start_node


@dataclass
class Chaos:
    primary: Node
    replica: Node
    other: Node  # group b, never faulted
    proxy: FaultProxy  # router, manager and replica -> primary
    router: Any

    @property
    def manager(self) -> ClusterManager:
        manager: ClusterManager = self.router.state.manager
        return manager

    def client(self, timeout_s: float = 5) -> KVClient:
        return KVClient("127.0.0.1", self.router.state.tcp_server.port, timeout_s=timeout_s)

    def key(self, group: str) -> str:
        router = self.router.state.router
        return next(k for k in (f"k{i}" for i in range(1000)) if router.owner(k) == group)

    def partition(self) -> None:
        self.proxy.partition()

    def heal(self) -> None:
        self.proxy.heal()


async def build(
    stack: AsyncExitStack, tmp_path: Path, *, min_replicas: int = 0, dead_after_s: float = 0.6
) -> Chaos:
    primary = await stack.enter_async_context(
        start_node(tmp_path, "p", min_replicas_to_write=min_replicas, min_replicas_max_lag_s=2.5)
    )
    proxy = FaultProxy("127.0.0.1", primary.app.state.tcp_server.port)
    await proxy.start()
    stack.push_async_callback(proxy.stop)
    replica = await stack.enter_async_context(start_node(tmp_path, "r", replicaof=proxy.address))
    other = await stack.enter_async_context(start_node(tmp_path, "o"))
    await in_sync(primary, replica)
    # Online, not just registered: min-replicas-to-write only counts synced replicas.
    await eventually(
        lambda: any(r.state == "online" for r in primary.node.primary.replicas.values())
    )
    settings = make_settings(
        tmp_path,
        node_role="router",
        data_dir=tmp_path / "router",
        shards=f"a={proxy.address}+{replica.address},b={other.address}",
        shard_timeout_s=0.5,
        heartbeat_interval_s=0.1,
        suspect_after_s=dead_after_s / 2,
        dead_after_s=dead_after_s,
    )
    router, _ = await stack.enter_async_context(running_app(settings))
    chaos = Chaos(primary, replica, other, proxy, router)
    await eventually(lambda: chaos.manager.is_healthy(replica.address))
    return chaos


@pytest.fixture
async def stack() -> AsyncIterator[AsyncExitStack]:
    async with AsyncExitStack() as stack:
        yield stack


# -------------------------------------------------------------- partition
async def write_until_refused(node: Node, seconds: float) -> tuple[int, list[str]]:
    """The minority side: a client still reaching the old primary keeps writing."""
    accepted: list[str] = []
    refused = 0
    deadline = time.monotonic() + seconds
    i = 0
    while time.monotonic() < deadline:
        key = f"minority{i}"
        i += 1
        try:
            node.run("SET", key, "x")
            accepted.append(key)
        except NotEnoughReplicasError:
            refused += 1
        await asyncio.sleep(0.02)
    return refused, accepted


@pytest.mark.parametrize("min_replicas", [0, 1])
async def test_partition_split_brain_is_fenced(
    stack: AsyncExitStack, tmp_path: Path, min_replicas: int
) -> None:
    chaos = await build(stack, tmp_path, min_replicas=min_replicas)
    key = chaos.key("a")
    async with chaos.client() as client:
        assert await client.execute("SET", key, "before") == "OK"
    await in_sync(chaos.primary, chaos.replica)

    chaos.partition()
    minority = asyncio.create_task(write_until_refused(chaos.primary, 5.0))
    await eventually(lambda: bool(chaos.manager.events), within_s=5)
    assert chaos.manager.events[0].new_primary == chaos.replica.address
    async with chaos.client() as client:  # the majority side carries on
        assert await client.execute("SET", key, "majority") == "OK"
    refused, accepted = await minority

    if min_replicas:
        # Fenced: once its replica's acks were older than the lag limit, the
        # isolated primary refused writes -- they would all have been lost.
        assert refused > 0
        assert refused > len(accepted) / 4  # fenced after ~2.5 s of a 5 s window
    else:
        assert refused == 0  # nothing stops it: every one of these is doomed

    chaos.heal()
    await eventually(lambda: chaos.primary.node.role == "replica", within_s=5)
    await in_sync(chaos.replica, chaos.primary)
    # One primary, the majority's writes kept, the minority's discarded.
    assert chaos.replica.node.role == "primary"
    assert chaos.primary.run("GET", key) == "majority"
    assert all(chaos.primary.run("GET", k) is None for k in accepted)
    assert chaos.primary.node.epoch == chaos.replica.node.epoch == 1


# ------------------------------------------------------------- slow node
async def test_latency_below_the_detection_threshold(stack: AsyncExitStack, tmp_path: Path) -> None:
    chaos = await build(stack, tmp_path, dead_after_s=1.0)
    slow, fast = chaos.key("a"), chaos.key("b")
    chaos.proxy.delay(0.1)
    async with chaos.client() as client:
        started = time.monotonic()
        assert await client.execute("SET", slow, "v") == "OK"
        slow_s = time.monotonic() - started
        started = time.monotonic()
        assert await client.execute("SET", fast, "v") == "OK"
        fast_s = time.monotonic() - started
    assert slow_s >= 0.17  # 100 ms each way (minus Windows timer resolution)
    assert fast_s < slow_s / 2  # the other group doesn't wait for the slow one
    await asyncio.sleep(1.5)
    assert not chaos.manager.events  # slow is not dead: no failover


async def test_a_node_too_slow_to_answer_is_failed_over(
    stack: AsyncExitStack, tmp_path: Path
) -> None:
    chaos = await build(stack, tmp_path, dead_after_s=0.6)
    chaos.proxy.delay(2.0)  # heartbeats time out long before this
    await eventually(lambda: bool(chaos.manager.events), within_s=5)
    assert chaos.manager.events[0].new_primary == chaos.replica.address


# -------------------------------------------------------------- disk full
async def test_a_full_disk_refuses_writes_but_nothing_else(
    stack: AsyncExitStack, tmp_path: Path
) -> None:
    chaos = await build(stack, tmp_path, dead_after_s=0.6)
    key, other_key = chaos.key("a"), chaos.key("b")
    async with chaos.client() as client:
        assert await client.execute("SET", key, "acked") == "OK"
        writer = chaos.primary.engine.persistence._writer  # type: ignore[union-attr]
        assert writer is not None

        def full(data: bytes) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

        writer._file.write = full  # type: ignore[method-assign]
        with pytest.raises(KVStoreError, match=r"No space left"):
            await client.execute("SET", key, "unacked")  # never acknowledged
        with pytest.raises(KVStoreError, match=r"Errors writing to the AOF"):
            await client.execute("SET", key, "refused")  # MISCONF from now on
        # Reads are still served. The failed write was applied in memory before
        # the disk refused it; under fsync no/everysec losing it is within the
        # policy (Redis behaves the same), but nobody was told it succeeded.
        assert await client.execute("GET", key) in ("acked", "unacked")
        assert await client.execute("SET", other_key, "fine") == "OK"  # other groups unaffected
    await asyncio.sleep(1.0)
    assert not chaos.manager.events  # reachable, so not failed over
