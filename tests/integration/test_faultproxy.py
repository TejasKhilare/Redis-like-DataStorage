"""The chaos tests' fault proxy: pass-through, latency, partition and heal."""

import asyncio
import time
from collections.abc import AsyncIterator

import pytest

from benchmarks.faultproxy import FaultProxy
from kvstore.core.exceptions import NodeUnavailableError
from kvstore.engine import Engine
from kvstore.protocol.client import KVClient
from kvstore.protocol.tcp_server import TCPServer


@pytest.fixture
async def proxied() -> AsyncIterator[tuple[FaultProxy, TCPServer]]:
    server = TCPServer(Engine().execute, host="127.0.0.1", port=0)
    await server.start()
    proxy = FaultProxy("127.0.0.1", server.port)
    await proxy.start()
    yield proxy, server
    await proxy.stop()
    await server.stop()


async def test_passes_traffic_through(proxied: tuple[FaultProxy, TCPServer]) -> None:
    proxy, _ = proxied
    async with KVClient("127.0.0.1", proxy.port) as client:
        assert await client.execute("SET", "a", "1") == "OK"
        replies = await client.pipeline([["GET", "a"]] * 100)
        assert replies == ["1"] * 100
    assert proxy.connections == 1


async def test_adds_latency_in_each_direction(proxied: tuple[FaultProxy, TCPServer]) -> None:
    proxy, _ = proxied
    async with KVClient("127.0.0.1", proxy.port) as client:
        assert await client.execute("PING") == "PONG"
        proxy.delay(0.1)
        started = time.monotonic()
        assert await client.execute("PING") == "PONG"
        assert time.monotonic() - started >= 0.2  # there and back
        proxy.delay(0)
        started = time.monotonic()
        assert await client.execute("PING") == "PONG"
        assert time.monotonic() - started < 0.1


async def test_a_partition_times_requests_out_and_heals(
    proxied: tuple[FaultProxy, TCPServer],
) -> None:
    proxy, _ = proxied
    async with KVClient("127.0.0.1", proxy.port, timeout_s=0.3) as client:
        assert await client.execute("PING") == "PONG"
        proxy.partition()
        with pytest.raises(NodeUnavailableError, match="TimeoutError"):
            await client.execute("PING")  # no reply, no reset: a timeout
        proxy.heal()
        assert await client.execute("PING") == "PONG"  # reconnects through the healed link


async def test_an_unreachable_target_closes_the_connection() -> None:
    proxy = FaultProxy("127.0.0.1", 1)
    await proxy.start()
    try:
        async with KVClient("127.0.0.1", proxy.port, timeout_s=3) as client:
            with pytest.raises(NodeUnavailableError):
                await client.execute("PING")
    finally:
        await proxy.stop()
    await asyncio.sleep(0)
