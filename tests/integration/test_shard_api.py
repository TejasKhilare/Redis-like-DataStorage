"""The shard's HTTP API, running with its full lifespan (engine, AOF, TCP server)."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from tests.helpers import make_settings, running_app


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    async with running_app(make_settings(tmp_path)) as (_, http):
        yield http


async def test_put_get_delete(client: httpx.AsyncClient) -> None:
    response = await client.put("/v1/keys/user:1", json={"value": '{"name": "tejas"}'})
    assert response.status_code == 200
    assert response.json() == {"key": "user:1", "ttl_seconds": None}

    response = await client.get("/v1/keys/user:1")
    assert response.status_code == 200
    assert response.json() == {"key": "user:1", "value": '{"name": "tejas"}'}
    assert (await client.get("/v1/keys/user:1/type")).json() == {"key": "user:1", "type": "string"}

    assert (await client.delete("/v1/keys/user:1")).status_code == 204
    assert (await client.get("/v1/keys/user:1")).status_code == 404


async def test_missing_key_uses_error_envelope(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/keys/nope")
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "KEY_NOT_FOUND", "message": "key 'nope' not found"}
    }
    assert (await client.delete("/v1/keys/nope")).status_code == 404


async def test_ttl_lifecycle(client: httpx.AsyncClient) -> None:
    await client.put("/v1/keys/session", json={"value": "abc", "ttl_seconds": 100})
    ttl = (await client.get("/v1/keys/session/ttl")).json()
    assert 99 <= ttl["ttl_seconds"] <= 100

    response = await client.put("/v1/keys/session/ttl", json={"seconds": 500})
    assert response.json() == {"key": "session", "ttl_seconds": 500}

    response = await client.delete("/v1/keys/session/ttl")
    assert response.json() == {"key": "session", "ttl_seconds": None}
    assert (await client.get("/v1/keys/session/ttl")).json()["ttl_seconds"] is None

    # Removing a TTL that isn't there is fine; a missing key is not.
    assert (await client.delete("/v1/keys/session/ttl")).status_code == 200
    assert (await client.delete("/v1/keys/nope/ttl")).status_code == 404
    assert (await client.get("/v1/keys/nope/ttl")).status_code == 404
    assert (await client.put("/v1/keys/nope/ttl", json={"seconds": 5})).status_code == 404


async def test_validation_errors(client: httpx.AsyncClient) -> None:
    response = await client.put("/v1/keys/a", json={"value": "1", "ttl_seconds": 0})
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert body["details"][0]["loc"] == ["body", "ttl_seconds"]

    assert (await client.put("/v1/keys/a", json={})).status_code == 422


async def test_values_must_be_strings(client: httpx.AsyncClient) -> None:
    response = await client.put("/v1/keys/a", json={"value": {"nested": 1}})
    assert response.status_code == 422


async def test_wrongtype_maps_to_400(client: httpx.AsyncClient) -> None:
    await client.post("/v1/commands", json={"command": "RPUSH", "args": ["l", "a"]})
    response = await client.get("/v1/keys/l")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "WRONG_TYPE"
    assert (await client.get("/v1/keys/l/type")).json()["type"] == "list"
    assert (await client.get("/v1/keys/nope/type")).status_code == 404


async def test_generic_command_endpoint(client: httpx.AsyncClient) -> None:
    body = {"command": "ZADD", "args": ["board", 10, "tejas", 2.5, "amol"]}
    assert (await client.post("/v1/commands", json=body)).json() == {"result": 2}
    body = {"command": "ZRANGE", "args": ["board", 0, -1, "WITHSCORES"]}
    assert (await client.post("/v1/commands", json=body)).json() == {
        "result": ["amol", "2.5", "tejas", "10"]
    }
    response = await client.post("/v1/commands", json={"command": "NOPE"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "UNKNOWN_COMMAND"
    assert (await client.post("/v1/commands", json={"command": ""})).status_code == 422


async def test_noeviction_maps_to_507(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, max_keys=1, eviction_policy="noeviction")
    async with running_app(settings) as (_, http):
        await http.put("/v1/keys/a", json={"value": "1"})
        response = await http.put("/v1/keys/b", json={"value": "1"})
        assert response.status_code == 507
        assert response.json()["error"]["code"] == "OUT_OF_MEMORY"


async def test_background_rewrite_endpoint(tmp_path: Path) -> None:
    async with running_app(make_settings(tmp_path)) as (app, http):
        await http.put("/v1/keys/a", json={"value": "1"})
        response = await http.post("/v1/admin/rewrite")
        assert response.status_code == 202
        app.state.engine.wait_rewrite()  # the copy runs in slices; wait for all of it
        persistence = (await http.get("/v1/admin/info")).json()["engine"]["persistence"]
        assert persistence["rewrites_completed"] == 1
        assert persistence["last_rewrite_status"] == "ok"


async def test_health_and_readiness(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health")).json() == {"status": "ok"}
    response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"engine": "ok", "persistence": "ok", "tcp": "ok"},
    }


async def test_request_id_is_echoed_or_generated(client: httpx.AsyncClient) -> None:
    response = await client.get("/health", headers={"X-Request-ID": "trace-123"})
    assert response.headers["X-Request-ID"] == "trace-123"
    assert len((await client.get("/health")).headers["X-Request-ID"]) == 32


async def test_admin_info(client: httpx.AsyncClient) -> None:
    await client.put("/v1/keys/a", json={"value": "1"})
    info = (await client.get("/v1/admin/info")).json()

    assert info["role"] == "shard"
    assert info["node_id"] == "shard-0"
    assert info["tcp_port"] > 0
    assert info["connected_clients"] == 0
    assert info["engine"]["keys"] == 1
    assert info["engine"]["used_memory"] > 0
    assert info["engine"]["persistence"]["aof_current_size"] > 0
    assert info["engine"]["persistence"]["aof_fsync"] == "no"


async def test_openapi_docs_are_served(client: httpx.AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()
    assert "/v1/keys/{key}" in schema["paths"]
    assert (await client.get("/docs")).status_code == 200


async def test_data_survives_restart(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    async with running_app(settings) as (_, http):
        await http.put("/v1/keys/persisted", json={"value": "yes"})

    async with running_app(settings) as (_, http):
        response = await http.get("/v1/keys/persisted")
        assert response.json()["value"] == "yes"


async def test_in_memory_mode_without_tcp(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, aof_enabled=False, tcp_enabled=False)
    async with running_app(settings) as (_, http):
        await http.put("/v1/keys/a", json={"value": "1"})
        info = (await http.get("/v1/admin/info")).json()
        assert info["tcp_port"] is None
        assert info["engine"]["persistence"] is None
        assert (await http.get("/ready")).json()["checks"] == {"engine": "ok"}
    assert not (tmp_path / "data").exists()
