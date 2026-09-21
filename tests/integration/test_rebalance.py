"""Adding and removing shard groups: only the keys that change owner move, under load."""

import asyncio
import random
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from kvstore.cluster.hash_ring import ConsistentHashRing
from kvstore.cluster.migration import KeyMigration, MigrationPlan
from kvstore.cluster.router import ShardRouter
from kvstore.core.exceptions import AskRedirectError, TryAgainError
from kvstore.engine import Engine
from kvstore.protocol.client import KVClient
from tests.helpers import make_settings, running_app
from tests.integration.test_replication import Node, start_node

KEYS = 3000


@dataclass
class Cluster:
    stack: AsyncExitStack
    tmp_path: Path
    nodes: dict[str, Node]
    router_app: Any
    http: httpx.AsyncClient

    @property
    def router(self) -> ShardRouter:
        router: ShardRouter = self.router_app.state.router
        return router

    def client(self) -> KVClient:
        return KVClient("127.0.0.1", self.router_app.state.tcp_server.port, timeout_s=10)

    async def add_node(self, name: str) -> Node:
        node = await self.stack.enter_async_context(start_node(self.tmp_path, name))
        self.nodes[name] = node
        return node

    def total_keys(self) -> int:
        return sum(len(node.engine.store) for node in self.nodes.values())


@pytest.fixture
async def cluster(tmp_path: Path) -> AsyncIterator[Cluster]:
    async with AsyncExitStack() as stack:
        nodes = {
            f"shard-{i}": await stack.enter_async_context(start_node(tmp_path, f"shard-{i}"))
            for i in (1, 2, 3)
        }
        settings = make_settings(
            tmp_path,
            node_role="router",
            data_dir=tmp_path / "router",
            shards=",".join(f"{name}={node.address}" for name, node in nodes.items()),
            heartbeat_interval_s=0.05,
        )
        app, http = await stack.enter_async_context(running_app(settings))
        cluster = Cluster(stack, tmp_path, nodes, app, http)
        async with cluster.client() as client:
            replies = await client.pipeline([["SET", f"k{i}", f"v{i}"] for i in range(KEYS)])
            assert replies == ["OK"] * KEYS
        yield cluster


def owners(ring_ids: list[str]) -> dict[str, str]:
    ring = ConsistentHashRing(ring_ids)
    return {f"k{i}": ring.get_node(f"k{i}") for i in range(KEYS)}


async def test_adding_a_group_moves_about_a_quarter_of_the_keys(cluster: Cluster) -> None:
    new = await cluster.add_node("shard-4")
    response = await cluster.http.post(
        "/v1/cluster/shards", json={"id": "shard-4", "primary": new.address}
    )
    assert response.status_code == 200, response.text
    result = response.json()

    before, after = (
        owners(["shard-1", "shard-2", "shard-3"]),
        owners(["shard-1", "shard-2", "shard-3", "shard-4"]),
    )
    expected = sum(before[k] != after[k] for k in before)
    assert result["moved_keys"] == expected  # exactly the keys whose owner changed
    assert 0.15 < expected / KEYS < 0.35  # ~1/4 with 4 groups
    assert all(after[k] == "shard-4" for k in before if before[k] != after[k])
    assert result["ring"] == ["shard-1", "shard-2", "shard-3", "shard-4"]

    # Every key is in exactly one place: its new owner.
    assert cluster.total_keys() == KEYS
    for name, node in cluster.nodes.items():
        assert {k for k in node.engine.store.iter_keys()} == {
            k for k, owner in after.items() if owner == name
        }
    async with cluster.client() as client:
        values = await client.pipeline([["GET", f"k{i}"] for i in range(KEYS)])
    assert values == [f"v{i}" for i in range(KEYS)]
    assert cluster.router.config.rebalance is None
    assert cluster.router.owner("k1") == after["k1"]


async def test_writes_during_a_rebalance_are_not_lost(cluster: Cluster) -> None:
    new = await cluster.add_node("shard-4")
    expected: dict[str, str] = {f"k{i}": f"v{i}" for i in range(KEYS)}
    stop = asyncio.Event()
    rng = random.Random(1)

    async def writer() -> int:
        writes = 0
        async with cluster.client() as client:
            while not stop.is_set():
                batch = [f"k{rng.randrange(KEYS + 500)}" for _ in range(20)]  # some are new
                values = [f"w{writes}-{j}" for j in range(len(batch))]
                replies = await client.pipeline(
                    [["SET", key, value] for key, value in zip(batch, values, strict=True)]
                )
                for key, value, reply in zip(batch, values, replies, strict=True):
                    assert reply == "OK", reply  # ASK / TRYAGAIN were handled by the router
                    expected[key] = value
                writes += 1
                await asyncio.sleep(0)
        return writes

    task = asyncio.create_task(writer())
    response = await cluster.http.post(
        "/v1/cluster/shards", json={"id": "shard-4", "primary": new.address}
    )
    stop.set()
    assert await task > 0
    assert response.status_code == 200, response.text

    async with cluster.client() as client:
        keys = sorted(expected)
        values = await client.pipeline([["GET", key] for key in keys])
    assert dict(zip(keys, values, strict=True)) == expected
    assert cluster.total_keys() == len(expected)  # nothing left behind, nothing doubled


async def test_removing_a_group_moves_all_its_keys(cluster: Cluster) -> None:
    on_2 = len(cluster.nodes["shard-2"].engine.store)
    response = await cluster.http.delete("/v1/cluster/shards/shard-2")
    assert response.status_code == 200, response.text
    assert response.json()["moved_keys"] == on_2
    assert response.json()["moved_by_shard"]["shard-2"] == on_2
    assert len(cluster.nodes["shard-2"].engine.store) == 0
    assert [g.id for g in cluster.router.config.shards] == ["shard-1", "shard-3"]
    async with cluster.client() as client:
        values = await client.pipeline([["GET", f"k{i}"] for i in range(KEYS)])
    assert values == [f"v{i}" for i in range(KEYS)]

    response = await cluster.http.delete("/v1/cluster/shards/nope")
    assert response.status_code == 400


async def test_rebalance_requests_are_validated(cluster: Cluster) -> None:
    response = await cluster.http.post(
        "/v1/cluster/shards", json={"id": "shard-1", "primary": "127.0.0.1:1"}
    )
    assert response.status_code == 502
    assert "already exists" in response.json()["error"]["message"]
    response = await cluster.http.post(
        "/v1/cluster/shards", json={"id": "x", "primary": "127.0.0.1:1"}
    )
    assert response.status_code == 502
    assert "not reachable" in response.json()["error"]["message"]


# ------------------------------------------------------------- migration
def test_migration_redirects_keys_by_where_they_are() -> None:
    engine = Engine()
    plan = MigrationPlan("a", 100, {"a": "h:1", "b": "h:2"})
    migration = KeyMigration(engine, plan)
    moving = next(f"k{i}" for i in range(100) if migration.destination(f"k{i}"))
    staying = next(f"k{i}" for i in range(100) if migration.destination(f"k{i}") is None)
    assert migration.destination(moving) == ("b", "h:2")

    migration.check("GET", [staying])  # ours: served here
    with pytest.raises(AskRedirectError) as info:
        migration.check("SET", [moving, "v"])  # not here, belongs to b: ask b
    assert info.value.target == "h:2"
    engine.execute("SET", moving, "v")
    migration.check("GET", [moving])  # still here: served here until moved
    migration._in_flight.add(moving)
    with pytest.raises(TryAgainError):
        migration.check("GET", [moving])
    migration._in_flight.clear()
    other = next(
        f"x{i}" for i in range(100) if migration.destination(f"x{i}") and f"x{i}" != moving
    )
    with pytest.raises(TryAgainError, match="multiple keys"):
        migration.check("MGET", [moving, other])  # one here, one moved
    migration.check("PING", [])  # keyless: never redirected


def test_migration_plan_round_trips_through_arguments() -> None:
    plan = MigrationPlan("a", 64, {"a": "h:1", "b": "h:2"})
    assert MigrationPlan.from_args(plan.to_args()) == plan
    with pytest.raises(ValueError, match="expected"):
        MigrationPlan.from_args(["a", 64, "b"])


def test_adding_a_node_only_moves_keys_to_it() -> None:
    old = ConsistentHashRing([f"s{i}" for i in range(5)])
    new = ConsistentHashRing([f"s{i}" for i in range(6)])
    keys = [f"key:{i}" for i in range(20_000)]
    moved = [k for k in keys if old.get_node(k) != new.get_node(k)]
    assert all(new.get_node(k) == "s5" for k in moved)
    assert 0.10 < len(moved) / len(keys) < 0.24  # ~1/6
