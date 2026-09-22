"""Failure detection, automatic and requested failover, and fencing a returning primary."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from kvstore.cluster.manager import ClusterManager, parse_info
from kvstore.cluster.router import ShardRouter
from kvstore.cluster.topology import ClusterConfig
from kvstore.core.exceptions import NotEnoughReplicasError
from kvstore.protocol.client import KVClient
from kvstore.protocol.tcp_server import TCPServer
from tests.helpers import make_settings, running_app
from tests.integration.test_replication import Node, eventually, in_sync, start_node


@dataclass
class Cluster:
    nodes: dict[str, Node]  # "a1" (primary of a), "a2" (replica of a), "b1", "b2"
    router_app: Any
    http: httpx.AsyncClient

    @property
    def router(self) -> ShardRouter:
        router: ShardRouter = self.router_app.state.router
        return router

    @property
    def manager(self) -> ClusterManager:
        manager: ClusterManager = self.router_app.state.manager
        return manager

    def client(self) -> KVClient:
        return KVClient("127.0.0.1", self.router_app.state.tcp_server.port, timeout_s=5)

    def key_in(self, shard: str) -> str:
        return next(k for k in (f"key{i}" for i in range(1000)) if self.router.owner(k) == shard)


@pytest.fixture
async def cluster(tmp_path: Path) -> AsyncIterator[Cluster]:
    async with AsyncExitStack() as stack:
        nodes: dict[str, Node] = {}
        for group in ("a", "b"):
            primary = await stack.enter_async_context(start_node(tmp_path, f"{group}1"))
            replica = await stack.enter_async_context(
                start_node(tmp_path, f"{group}2", replicaof=primary.address)
            )
            await in_sync(primary, replica)
            nodes[f"{group}1"], nodes[f"{group}2"] = primary, replica
        spec = [f"{g}={nodes[g + '1'].address}+{nodes[g + '2'].address}" for g in ("a", "b")]
        settings = make_settings(
            tmp_path,
            node_role="router",
            data_dir=tmp_path / "router",
            shards=",".join(spec),
            shard_timeout_s=1,
            heartbeat_interval_s=0.05,
            suspect_after_s=0.15,
            dead_after_s=0.3,
        )
        app, http = await stack.enter_async_context(running_app(settings))
        cluster = Cluster(nodes, app, http)
        await eventually(lambda: len(cluster.manager.health) == 4)
        yield cluster


async def crash(node: Node) -> int:
    """Stop answering, like a killed process: the port closes, nothing more is sent."""
    port: int = node.app.state.tcp_server.port
    node.node.primary.disconnect_all()
    await node.app.state.tcp_server.stop()
    return port


async def test_automatic_failover_promotes_the_replica(cluster: Cluster) -> None:
    key = cluster.key_in("a")
    async with cluster.client() as client:
        assert await client.execute("SET", key, "before") == "OK"
        await in_sync(cluster.nodes["a1"], cluster.nodes["a2"])

        await crash(cluster.nodes["a1"])
        crashed_at = time.monotonic()
        while True:  # keep writing, as a client would, until the group is back
            try:
                assert await client.execute("SET", key, "after") == "OK"
                break
            except Exception:
                await asyncio.sleep(0.02)
        recovered_in = time.monotonic() - crashed_at

    assert recovered_in < 3
    (event,) = cluster.manager.events
    assert event.shard == "a"
    assert event.new_primary == cluster.nodes["a2"].address
    assert event.epoch == 1
    assert cluster.router.config.epoch == 1
    assert cluster.router.primary("a") == cluster.nodes["a2"].address
    assert cluster.nodes["a2"].node.role == "primary"
    assert cluster.nodes["a2"].run("GET", key) == "after"
    # The other group was not touched.
    assert cluster.router.primary("b") == cluster.nodes["b1"].address
    await cluster.manager.flush_config()
    saved = ClusterConfig.load(cluster.router_app.state.settings.data_dir / "cluster.json")
    assert saved is not None and saved.epoch == 1


async def test_a_returning_primary_is_fenced_and_resynced(cluster: Cluster) -> None:
    old = cluster.nodes["a1"]
    key = cluster.key_in("a")
    port = await crash(old)
    await eventually(lambda: bool(cluster.manager.events))
    new_primary = cluster.nodes["a2"]
    assert new_primary.run("SET", key, "new") == "OK"

    # Before anyone tells it otherwise, the old primary still believes it leads
    # -- and takes a write that nobody else has.
    assert old.node.role == "primary"
    assert old.run("SET", "stale", "x") == "OK"

    # It comes back on the same address.
    server = TCPServer(
        old.node.execute, host="127.0.0.1", port=port, group_commit=old.app.state.group_commit
    )
    await server.start()
    old.app.state.tcp_server = server
    try:
        await eventually(lambda: old.node.role == "replica")
        await in_sync(new_primary, old)
        assert old.run("GET", key) == "new"
        assert old.run("GET", "stale") is None  # its divergent write is gone
        assert old.node.epoch == 1
        info = parse_info(old.run("INFO", "replication"))
        assert info["master_port"] == new_primary.address.split(":")[1]
    finally:
        await server.stop()


async def test_requested_failover_over_http(cluster: Cluster) -> None:
    key = cluster.key_in("b")
    async with cluster.client() as client:
        assert await client.execute("SET", key, "v") == "OK"
    response = await cluster.http.post("/v1/cluster/shards/b/failover")
    assert response.status_code == 200
    body = response.json()
    assert body["new_primary"] == cluster.nodes["b2"].address
    assert body["reason"] == "requested"
    # Nothing was lost: the replica caught up before it was promoted.
    assert cluster.nodes["b2"].run("GET", key) == "v"
    await eventually(lambda: cluster.nodes["b1"].node.role == "replica")

    config = (await cluster.http.get("/v1/cluster/config")).json()
    assert config["epoch"] == 1
    group_b = next(g for g in config["shards"] if g["id"] == "b")
    assert group_b["primary"] == cluster.nodes["b2"].address
    assert group_b["replicas"] == [cluster.nodes["b1"].address]
    events = (await cluster.http.get("/v1/cluster/events")).json()
    assert [e["shard"] for e in events] == ["b"]

    nodes = (await cluster.http.get("/v1/cluster/nodes")).json()
    assert nodes["epoch"] == 1
    states = {n["address"]: n for n in nodes["nodes"]}
    assert states[cluster.nodes["b2"].address]["role"] == "primary"
    assert states[cluster.nodes["b2"].address]["state"] == "healthy"


async def test_failover_needs_a_replica(tmp_path: Path) -> None:
    async with start_node(tmp_path, "solo") as solo:
        settings = make_settings(
            tmp_path, node_role="router", data_dir=tmp_path / "r", shards=solo.address
        )
        async with running_app(settings) as (_, http):
            response = await http.post("/v1/cluster/shards/shard-1/failover")
            assert response.status_code == 502
            assert "no healthy replica" in response.json()["error"]["message"]


async def test_write_concern_acknowledges_only_replicated_writes(tmp_path: Path) -> None:
    async with AsyncExitStack() as stack:
        primary = await stack.enter_async_context(start_node(tmp_path, "p"))
        replica = await stack.enter_async_context(
            start_node(tmp_path, "r", replicaof=primary.address)
        )
        await in_sync(primary, replica)
        settings = make_settings(
            tmp_path,
            node_role="router",
            data_dir=tmp_path / "router",
            shards=f"a={primary.address}+{replica.address}",
            wait_replicas=1,
            wait_timeout_s=0.3,
            failover_enabled=False,
        )
        app, _ = await stack.enter_async_context(running_app(settings))
        async with KVClient("127.0.0.1", app.state.tcp_server.port, timeout_s=5) as client:
            assert await client.execute("SET", "k", "v") == "OK"
            assert replica.run("GET", "k") == "v"  # already there when we were told OK
            assert await client.execute("GET", "k") == "v"  # reads don't wait

            replica.node.link.cancel()  # type: ignore[union-attr]
            primary.node.primary.disconnect_all()
            with pytest.raises(NotEnoughReplicasError, match="0 of 1"):
                await client.execute("SET", "k", "unconfirmed")


async def test_reads_can_be_served_by_healthy_replicas(tmp_path: Path) -> None:
    async with AsyncExitStack() as stack:
        primary = await stack.enter_async_context(start_node(tmp_path, "p"))
        replica = await stack.enter_async_context(
            start_node(tmp_path, "r", replicaof=primary.address)
        )
        await in_sync(primary, replica)
        settings = make_settings(
            tmp_path,
            node_role="router",
            data_dir=tmp_path / "router",
            shards=f"a={primary.address}+{replica.address}",
            read_from_replicas=True,
            heartbeat_interval_s=0.05,
        )
        app, _ = await stack.enter_async_context(running_app(settings))
        manager: ClusterManager = app.state.manager
        await eventually(lambda: manager.is_healthy(replica.address))
        async with KVClient("127.0.0.1", app.state.tcp_server.port, timeout_s=5) as client:
            assert await client.execute("SET", "k", "v") == "OK"  # writes: the primary
            await in_sync(primary, replica)
            hits = primary.engine.info().keyspace_hits
            for _ in range(5):
                assert await client.execute("GET", "k") == "v"
            assert replica.engine.info().keyspace_hits == 5  # reads: the replica
            assert primary.engine.info().keyspace_hits == hits

            # A replica whose link is down may be stale: reads go back to the primary.
            replica.node.link.cancel()  # type: ignore[union-attr]
            await eventually(lambda: not manager.is_healthy(replica.address))
            assert await client.execute("GET", "k") == "v"
            assert primary.engine.info().keyspace_hits == hits + 1
