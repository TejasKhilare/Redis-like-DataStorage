"""Router in front of three real shard TCP servers."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from kvstore.cluster.router import ShardRouter
from kvstore.core.exceptions import (
    CommandError,
    CrossShardError,
    InvalidArgumentError,
    NodeUnavailableError,
    UnknownCommandError,
    WrongArityError,
)
from kvstore.engine import Engine
from kvstore.protocol.client import KVClient
from kvstore.protocol.tcp_server import TCPServer
from tests.helpers import make_settings, running_app


@dataclass
class Shard:
    engine: Engine
    server: TCPServer

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.server.port}"


async def start_shard() -> Shard:
    engine = Engine()

    async def handler(command: str, *args: Any) -> Any:
        return engine.execute(command, *args)

    server = TCPServer(handler, host="127.0.0.1", port=0)
    await server.start()
    return Shard(engine, server)


@pytest.fixture
async def shards() -> AsyncIterator[dict[str, Shard]]:
    started = [await start_shard() for _ in range(3)]
    yield {shard.address: shard for shard in started}
    for shard in started:
        await shard.server.stop()


@pytest.fixture
async def router_app(
    tmp_path: Path, shards: dict[str, Shard]
) -> AsyncIterator[tuple[Any, httpx.AsyncClient]]:
    settings = make_settings(
        tmp_path, node_role="router", shards=",".join(shards), shard_timeout_s=1
    )
    async with running_app(settings) as app_and_client:
        yield app_and_client


async def test_http_writes_land_on_the_owning_shard(
    router_app: tuple[Any, httpx.AsyncClient], shards: dict[str, Shard]
) -> None:
    app, http = router_app
    for i in range(30):
        assert (await http.put(f"/v1/keys/user:{i}", json={"value": i})).status_code == 200

    for i in range(30):
        key = f"user:{i}"
        owner = (await http.get(f"/v1/cluster/keys/{key}/owner")).json()["node"]
        assert owner == app.state.router.owner(key)
        assert shards[owner].engine.execute("GET", key) == i
        assert (await http.get(f"/v1/keys/{key}")).json()["value"] == i

    # Keys actually spread across the cluster.
    assert all(shard.engine.execute("DBSIZE") > 0 for shard in shards.values())


async def test_router_tcp_proxy(router_app: tuple[Any, httpx.AsyncClient]) -> None:
    app, _ = router_app
    async with KVClient("127.0.0.1", app.state.tcp_server.port) as client:
        assert await client.execute("PING") == "PONG"
        assert await client.execute("SET", "a", "1", "EX", 100) == "OK"
        assert await client.execute("GET", "a") == "1"
        assert await client.execute("TTL", "a") == 100
        with pytest.raises(WrongArityError):
            await client.execute("GET")


async def test_router_rejects_what_it_cannot_route(shards: dict[str, Shard]) -> None:
    router = ShardRouter(list(shards), timeout_s=1)
    try:
        keys_by_owner: dict[str, str] = {}
        for i in range(100):
            keys_by_owner.setdefault(router.owner(f"k{i}"), f"k{i}")
        a, b, *_ = keys_by_owner.values()

        with pytest.raises(CrossShardError):
            await router.execute("DEL", a, b)
        with pytest.raises(UnknownCommandError):
            await router.execute("FLY")
        with pytest.raises(CommandError, match="not supported through the router"):
            await router.execute("DBSIZE")
        with pytest.raises(InvalidArgumentError):
            await router.execute("GET", 42)
        assert await router.execute("PING", "hi") == "hi"
        assert await router.execute("DEL", a, a) == 0  # same shard: allowed
    finally:
        await router.close()


async def test_cluster_nodes_and_readiness(
    router_app: tuple[Any, httpx.AsyncClient], shards: dict[str, Shard]
) -> None:
    _, http = router_app
    body = (await http.get("/v1/cluster/nodes")).json()
    assert body["virtual_nodes"] == 100
    assert {n["address"] for n in body["nodes"]} == set(shards)
    assert all(n["healthy"] for n in body["nodes"])

    ready = await http.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"

    info = (await http.get("/v1/admin/info")).json()
    assert info["role"] == "router"
    assert info["shards"] == sorted(shards)
    assert info["engine"] is None


async def test_shard_failure_is_isolated(
    router_app: tuple[Any, httpx.AsyncClient], shards: dict[str, Shard]
) -> None:
    app, http = router_app
    down_address, down = next(iter(shards.items()))
    await down.server.stop()

    candidates = [f"k{i}" for i in range(1000)]
    key_on_down = next(k for k in candidates if app.state.router.owner(k) == down_address)
    key_on_up = next(k for k in candidates if app.state.router.owner(k) != down_address)

    response = await http.put(f"/v1/keys/{key_on_down}", json={"value": 1})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "NODE_UNAVAILABLE"
    assert (await http.put(f"/v1/keys/{key_on_up}", json={"value": 1})).status_code == 200

    ready = (await http.get("/ready")).json()
    assert ready["status"] == "degraded"
    assert ready["checks"][down_address] == "down"

    nodes = {n["address"]: n for n in (await http.get("/v1/cluster/nodes")).json()["nodes"]}
    assert nodes[down_address]["healthy"] is False
    assert nodes[down_address]["error"]


async def test_all_shards_down_is_unavailable(
    router_app: tuple[Any, httpx.AsyncClient], shards: dict[str, Shard]
) -> None:
    _, http = router_app
    for shard in shards.values():
        await shard.server.stop()

    response = await http.get("/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


async def test_unreachable_shard_raises_node_unavailable() -> None:
    router = ShardRouter(["127.0.0.1:1"], timeout_s=0.5)
    try:
        with pytest.raises(NodeUnavailableError):
            await router.execute("GET", "a")
    finally:
        await router.close()
