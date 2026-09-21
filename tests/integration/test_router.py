"""Router in front of three real shard TCP servers."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from kvstore.cluster.hash_ring import hash_slot_key
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
    server = TCPServer(engine.execute, host="127.0.0.1", port=0, batch=engine.deferred_commit)
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


def test_hash_tags() -> None:
    assert hash_slot_key("{user:1}:cart") == "user:1"
    assert hash_slot_key("plain") == "plain"
    assert hash_slot_key("{}empty") == "{}empty"  # empty tag: the whole key is hashed
    assert hash_slot_key("a{b}c{d}") == "b"


async def test_http_writes_land_on_the_owning_shard(
    router_app: tuple[Any, httpx.AsyncClient], shards: dict[str, Shard]
) -> None:
    app, http = router_app
    for i in range(30):
        assert (await http.put(f"/v1/keys/user:{i}", json={"value": str(i)})).status_code == 200
    for i in range(30):
        key = f"user:{i}"
        owner = (await http.get(f"/v1/cluster/keys/{key}/owner")).json()
        assert owner["shard"] == app.state.router.owner(key)
        assert shards[owner["primary"]].engine.execute("GET", key) == str(i)
        assert (await http.get(f"/v1/keys/{key}")).json()["value"] == str(i)
    assert all(shard.engine.execute("DBSIZE") > 0 for shard in shards.values())


async def test_router_tcp_proxy(router_app: tuple[Any, httpx.AsyncClient]) -> None:
    app, _ = router_app
    async with KVClient("127.0.0.1", app.state.tcp_server.port) as client:
        assert await client.execute("PING") == "PONG"
        assert await client.execute("ECHO", "hi") == "hi"  # answered by the router itself
        assert await client.execute("SET", "a", "1", "EX", "100") == "OK"
        assert await client.execute("TTL", "a") == 100
        assert await client.execute("ZADD", "z", "1", "m") == 1
        assert await client.execute("ZRANGE", "z", "0", "-1", "WITHSCORES") == ["m", "1"]
        with pytest.raises(CommandError, match="wrong number of arguments"):
            await client.execute("GET")
        with pytest.raises(CommandError, match=r"wrong kind of value"):
            await client.execute("LPUSH", "a", "x")


async def test_fanout_commands(
    router_app: tuple[Any, httpx.AsyncClient], shards: dict[str, Shard]
) -> None:
    app, _ = router_app
    router: ShardRouter = app.state.router
    for i in range(40):
        await router.execute("SET", f"k{i}", "v")
    assert await router.execute("DBSIZE") == 40
    assert sorted(await router.execute("KEYS", "k1*")) == sorted(
        f"k{i}" for i in range(40) if str(i).startswith("1")
    )
    assert await router.execute("FLUSHALL") == "OK"
    assert await router.execute("DBSIZE") == 0


async def test_router_rejects_what_it_cannot_route(shards: dict[str, Shard]) -> None:
    router = ShardRouter(list(shards), timeout_s=1)
    try:
        keys_by_owner: dict[str, str] = {}
        for i in range(100):
            keys_by_owner.setdefault(router.owner(f"k{i}"), f"k{i}")
        a, b, *_ = keys_by_owner.values()

        with pytest.raises(CrossShardError) as info:
            await router.execute("DEL", a, b)
        assert info.value.to_resp().startswith("CROSSSLOT")
        with pytest.raises(UnknownCommandError):
            await router.execute("FLY")
        with pytest.raises(WrongArityError):
            await router.execute("GET")
        with pytest.raises(CommandError, match="not supported through the router"):
            await router.execute("INFO")
        with pytest.raises(InvalidArgumentError):
            await router.execute("GET", 42)
        assert await router.execute("PING", "hi") == "hi"
        assert await router.execute("DEL", a, a) == 0  # same shard: allowed
        assert await router.execute("MSET", "{t}:1", "x", "{t}:2", "y") == "OK"  # hash tag
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
    assert ready.status_code == 200 and ready.json()["status"] == "ready"
    info = (await http.get("/v1/admin/info")).json()
    assert info["role"] == "router"
    assert info["shards"] == ["shard-1", "shard-2", "shard-3"]  # stable group ids
    assert info["engine"] is None


async def test_http_commands_and_rewrite_through_router(
    router_app: tuple[Any, httpx.AsyncClient],
) -> None:
    _, http = router_app
    response = await http.post("/v1/commands", json={"command": "ZADD", "args": ["b", 5, "x"]})
    assert response.json() == {"result": 1}
    response = await http.post("/v1/commands", json={"command": "DBSIZE"})
    assert response.json() == {"result": 1}
    # The in-memory test shards have no persistence: the fan-out reports it.
    response = await http.post("/v1/admin/rewrite")
    assert response.status_code == 400
    assert "persistence is disabled" in response.json()["error"]["message"]


async def test_shard_failure_is_isolated(
    router_app: tuple[Any, httpx.AsyncClient], shards: dict[str, Shard]
) -> None:
    app, http = router_app
    down_address, down = next(iter(shards.items()))
    await down.server.stop()

    candidates = [f"k{i}" for i in range(1000)]
    router: ShardRouter = app.state.router
    key_on_down = next(k for k in candidates if router.primary(router.owner(k)) == down_address)
    key_on_up = next(k for k in candidates if router.primary(router.owner(k)) != down_address)

    response = await http.put(f"/v1/keys/{key_on_down}", json={"value": "1"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "NODE_UNAVAILABLE"
    assert (await http.put(f"/v1/keys/{key_on_up}", json={"value": "1"})).status_code == 200

    ready = (await http.get("/ready")).json()
    assert ready["status"] == "degraded"
    assert ready["checks"][down_address] == "down"
    nodes = {n["address"]: n for n in (await http.get("/v1/cluster/nodes")).json()["nodes"]}
    assert nodes[down_address]["healthy"] is False

    async with KVClient("127.0.0.1", app.state.tcp_server.port) as client:
        with pytest.raises(NodeUnavailableError):  # CLUSTERDOWN over RESP
            await client.execute("GET", key_on_down)


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
