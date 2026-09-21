"""The TCP data plane: wire protocol, pipelining, limits, client behaviour."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from kvstore.core.exceptions import NodeUnavailableError, UnknownCommandError, WrongArityError
from kvstore.engine import Engine
from kvstore.protocol.client import KVClient
from kvstore.protocol.tcp_server import TCPServer
from tests.helpers import make_settings, running_app


@pytest.fixture
async def shard_port(tmp_path: Path) -> AsyncIterator[int]:
    async with running_app(make_settings(tmp_path, max_request_bytes=4096)) as (app, _):
        yield app.state.tcp_server.port


async def test_client_round_trip(shard_port: int) -> None:
    async with KVClient("127.0.0.1", shard_port) as client:
        assert await client.execute("SET", "k", {"nested": [1, 2]}) == "OK"
        assert await client.execute("GET", "k") == {"nested": [1, 2]}
        assert await client.execute("DEL", "k") == 1


async def test_errors_come_back_typed_and_keep_the_connection(shard_port: int) -> None:
    async with KVClient("127.0.0.1", shard_port) as client:
        with pytest.raises(UnknownCommandError):
            await client.execute("FLY")
        with pytest.raises(WrongArityError):
            await client.execute("GET")
        assert await client.execute("PING") == "PONG"


async def test_pipelining(shard_port: int) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", shard_port)
    requests = [{"command": "SET", "args": [f"k{i}", i]} for i in range(50)]
    requests += [{"command": "DBSIZE"}]
    writer.write(b"".join(json.dumps(r).encode() + b"\n" for r in requests))
    await writer.drain()

    replies = [json.loads(await reader.readline()) for _ in requests]
    writer.close()
    await writer.wait_closed()

    assert all(r == {"ok": True, "result": "OK"} for r in replies[:-1])
    assert replies[-1] == {"ok": True, "result": 50}


async def test_malformed_lines_get_protocol_errors(shard_port: int) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", shard_port)
    writer.write(b"this is not json\n\n" + b'{"command": "PING"}\n')
    await writer.drain()

    first = json.loads(await reader.readline())
    second = json.loads(await reader.readline())  # blank line was skipped
    writer.close()
    await writer.wait_closed()

    assert first["error"]["code"] == "PROTOCOL_ERROR"
    assert second == {"ok": True, "result": "PONG"}


async def test_oversized_request_is_rejected_and_connection_closed(shard_port: int) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", shard_port)
    writer.write(b'{"command": "SET", "args": ["k", "' + b"x" * 10_000 + b'"]}\n')
    await writer.drain()

    reply = json.loads(await reader.readline())
    assert reply["error"]["code"] == "PROTOCOL_ERROR"
    assert "exceeds 4096 bytes" in reply["error"]["message"]
    assert await reader.read() == b""  # server hung up
    writer.close()


async def test_unexpected_handler_errors_become_internal_errors() -> None:
    async def broken(command: str, *args: object) -> object:
        raise RuntimeError("boom")

    server = TCPServer(broken, host="127.0.0.1", port=0)
    await server.start()
    try:
        async with KVClient("127.0.0.1", server.port) as client:
            with pytest.raises(Exception, match="internal error"):
                await client.execute("PING")
    finally:
        await server.stop()
    await server.stop()  # idempotent
    assert not server.is_serving
    with pytest.raises(RuntimeError, match="not running"):
        _ = server.port


async def test_stop_disconnects_idle_clients() -> None:
    engine = Engine()

    async def handler(command: str, *args: object) -> object:
        return engine.execute(command, *args)

    server = TCPServer(handler, host="127.0.0.1", port=0)
    await server.start()
    client = KVClient("127.0.0.1", server.port, timeout_s=1)
    assert await client.execute("PING") == "PONG"  # connection now open and idle

    await asyncio.wait_for(server.stop(), timeout=3)  # must not hang on the idle client

    with pytest.raises(NodeUnavailableError):
        await client.execute("PING")
    await client.close()


async def test_client_reports_unreachable_node_and_recovers() -> None:
    engine = Engine()

    async def handler(command: str, *args: object) -> object:
        return engine.execute(command, *args)

    server = TCPServer(handler, host="127.0.0.1", port=0)
    await server.start()
    port = server.port
    await server.stop()

    client = KVClient("127.0.0.1", port, timeout_s=1)
    with pytest.raises(NodeUnavailableError, match=f"127.0.0.1:{port} unavailable"):
        await client.execute("PING")

    # Same port comes back: the next call reconnects transparently.
    server = TCPServer(handler, host="127.0.0.1", port=port)
    await server.start()
    try:
        assert await client.execute("PING") == "PONG"
    finally:
        await client.close()
        await server.stop()


async def test_client_timeout_drops_the_connection() -> None:
    async def slow(command: str, *args: object) -> object:
        await asyncio.sleep(0.5)
        return "late"

    server = TCPServer(slow, host="127.0.0.1", port=0)
    await server.start()
    try:
        async with KVClient("127.0.0.1", server.port, timeout_s=0.1) as client:
            with pytest.raises(NodeUnavailableError, match="TimeoutError"):
                await client.execute("PING")
            # A fresh connection is used, so the late reply can't be misread.
            client.timeout_s = 2
            assert await client.execute("PING") == "late"
    finally:
        await server.stop()
