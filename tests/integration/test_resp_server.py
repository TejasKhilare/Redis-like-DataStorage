"""The RESP data plane at the socket level: framing, pipelining, group commit, client behaviour."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from kvstore.core.exceptions import (
    CommandError,
    KVStoreError,
    NodeUnavailableError,
)
from kvstore.engine import Engine
from kvstore.protocol.client import KVClient
from kvstore.protocol.resp import encode_command
from kvstore.protocol.tcp_server import TCPServer
from tests.helpers import make_settings, running_app


@pytest.fixture
async def shard(tmp_path: Path) -> AsyncIterator[Any]:
    settings = make_settings(tmp_path, max_request_bytes=4096, aof_fsync="always")
    async with running_app(settings) as (app, _):
        yield app


async def raw_exchange(port: int, payload: bytes, expect: int) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(payload)
    await writer.drain()
    data = b""
    while data.count(b"\r\n") < expect:
        chunk = await asyncio.wait_for(reader.read(65536), 5)
        if not chunk:
            break
        data += chunk
    writer.close()
    return data


async def test_client_round_trip(shard: Any) -> None:
    async with KVClient("127.0.0.1", shard.state.tcp_server.port) as client:
        assert await client.execute("SET", "k", "v") == "OK"
        assert await client.execute("GET", "k") == "v"
        assert await client.execute("LRANGE", "missing", 0, -1) == []
        with pytest.raises(CommandError, match="unknown command"):  # remote ERR -> CommandError
            await client.execute("FLY")
        assert await client.execute("PING") == "PONG"


async def test_inline_and_multibulk_on_one_connection(shard: Any) -> None:
    payload = b"PING\r\n" + encode_command(["SET", "a", "1"]) + b"GET a\r\n"
    data = await raw_exchange(shard.state.tcp_server.port, payload, expect=4)
    assert data == b"+PONG\r\n+OK\r\n$1\r\n1\r\n"


async def test_pipelined_writes_share_one_fsync(shard: Any) -> None:
    """Group commit: 200 pipelined SETs in one read -> one fsync (appendfsync always)."""
    engine: Engine = shard.state.engine
    stats = engine.info().persistence
    assert stats is not None
    fsyncs_before = stats.aof_fsyncs
    payload = b"".join(encode_command(["SET", f"k{i}", "v"]) for i in range(200))
    data = await raw_exchange(shard.state.tcp_server.port, payload, expect=200)
    assert data == b"+OK\r\n" * 200
    stats = engine.info().persistence
    assert stats is not None
    assert stats.aof_fsyncs - fsyncs_before < 20  # usually 1; TCP may split the payload
    assert engine.execute("DBSIZE") == 200


async def test_protocol_error_closes_the_connection(shard: Any) -> None:
    data = await raw_exchange(shard.state.tcp_server.port, b"*1\r\n:oops\r\n", expect=1)
    assert data.startswith(b"-ERR Protocol error: expected '$'")


async def test_oversized_bulk_is_rejected(shard: Any) -> None:
    payload = b"*3\r\n$3\r\nSET\r\n$1\r\nk\r\n$100000\r\n"
    data = await raw_exchange(shard.state.tcp_server.port, payload, expect=1)
    assert data == b"-ERR Protocol error: invalid bulk length\r\n"


async def test_quit(shard: Any) -> None:
    data = await raw_exchange(shard.state.tcp_server.port, b"QUIT\r\nPING\r\n", expect=2)
    assert data == b"+OK\r\n"  # nothing after QUIT is processed


async def test_failed_group_commit_drops_the_connection(shard: Any) -> None:
    engine: Engine = shard.state.engine
    assert engine.persistence is not None and engine.persistence._writer is not None
    engine.persistence._writer.background_error = OSError("disk gone")
    data = await raw_exchange(shard.state.tcp_server.port, encode_command(["GET", "x"]), expect=1)
    assert data == b""  # no reply may claim success for an un-durable batch
    async with KVClient("127.0.0.1", shard.state.tcp_server.port) as client:
        with pytest.raises(KVStoreError, match="Errors writing to the AOF"):
            await client.execute("SET", "a", "1")


async def test_unexpected_handler_errors_become_internal_errors() -> None:
    def broken(command: str, *args: object) -> object:
        raise RuntimeError("boom")

    server = TCPServer(broken, host="127.0.0.1", port=0)
    await server.start()
    try:
        async with KVClient("127.0.0.1", server.port) as client:
            with pytest.raises(CommandError, match="internal error"):
                await client.execute("PING")
    finally:
        await server.stop()
    await server.stop()  # idempotent
    with pytest.raises(RuntimeError, match="not running"):
        _ = server.port


async def test_stop_disconnects_idle_clients() -> None:
    server = TCPServer(Engine().execute, host="127.0.0.1", port=0)
    await server.start()
    client = KVClient("127.0.0.1", server.port, timeout_s=1)
    assert await client.execute("PING") == "PONG"
    assert server.connected_clients == 1
    await asyncio.wait_for(server.stop(), timeout=3)
    with pytest.raises(NodeUnavailableError):
        await client.execute("PING")
    await client.close()


async def test_client_reconnects_after_node_returns() -> None:
    engine = Engine()
    server = TCPServer(engine.execute, host="127.0.0.1", port=0)
    await server.start()
    port = server.port
    await server.stop()

    client = KVClient("127.0.0.1", port, timeout_s=1)
    with pytest.raises(NodeUnavailableError, match=f"127.0.0.1:{port} unavailable"):
        await client.execute("PING")
    server = TCPServer(engine.execute, host="127.0.0.1", port=port)
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
            client.timeout_s = 2
            assert await client.execute("PING") == "late"  # a fresh connection, not the stale reply
    finally:
        await server.stop()


async def test_client_pipeline_returns_errors_in_place() -> None:
    server = TCPServer(Engine().execute, host="127.0.0.1", port=0)
    await server.start()
    try:
        async with KVClient("127.0.0.1", server.port) as client:
            replies = await client.pipeline([["SET", "a", "1"], ["LPUSH", "a", "x"], ["GET", "a"]])
            assert replies[0] == "OK"
            assert isinstance(replies[1], KVStoreError) and replies[1].prefix == "WRONGTYPE"
            assert replies[2] == "1"
    finally:
        await server.stop()
