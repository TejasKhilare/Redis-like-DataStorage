"""The multiplexed client, the router's batching and retries, and cross-client group commit."""

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from kvstore.cluster.router import ShardRouter
from kvstore.cluster.topology import ClusterConfig, ShardGroup
from kvstore.core.exceptions import (
    AskRedirectError,
    ConnectFailedError,
    KVStoreError,
    NodeUnavailableError,
    ReadOnlyReplicaError,
    TryAgainError,
)
from kvstore.engine import Engine
from kvstore.protocol.client import KVClient, KVClientPool
from kvstore.protocol.resp import encode_command
from kvstore.protocol.tcp_server import GroupCommit, TCPServer
from tests.helpers import make_settings, running_app


class CountingServer:
    """A shard whose TCP reads are counted, to observe how requests are batched."""

    def __init__(self, handler: Callable[..., Any] | None = None) -> None:
        self.engine = Engine()
        self.reads = 0
        self.calls: list[list[str]] = []
        self._handler = handler
        self.server = TCPServer(self._handle, host="127.0.0.1", port=0)
        self.server._run = self._counted(self.server._run)  # type: ignore[method-assign]

    def _counted(self, run: Callable[[list[list[str]]], Any]) -> Callable[..., Any]:
        async def wrapper(commands: list[list[str]]) -> Any:
            self.reads += 1
            return await run(commands)

        return wrapper

    def _handle(self, *command: str) -> Any:
        self.calls.append(list(command))
        if self._handler is not None:
            return self._handler(self, *command)
        return self.engine.execute(*command)

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.server.port}"


@pytest.fixture
async def servers() -> AsyncIterator[list[CountingServer]]:
    """Servers started by :func:`start_server`, stopped after the test."""
    started: list[CountingServer] = []
    yield started
    for server in started:
        await server.server.stop()


async def start_server(servers: list[CountingServer], handler: Any = None) -> CountingServer:
    server = CountingServer(handler)
    await server.server.start()
    servers.append(server)
    return server


# ----------------------------------------------------------------- client
async def test_concurrent_requests_share_one_connection_and_write(
    servers: list[CountingServer],
) -> None:
    server = await start_server(servers)
    async with KVClient("127.0.0.1", server.server.port) as client:
        assert await client.execute("PING") == "PONG"  # connected: the steady state
        server.reads = 0
        replies = await asyncio.gather(*(client.execute("SET", f"k{i}", str(i)) for i in range(50)))
        assert replies == ["OK"] * 50
        assert server.server.connections_received == 1
        assert server.reads <= 3  # coalesced into (nearly) one write, not 50 round trips
        values = await asyncio.gather(*(client.execute("GET", f"k{i}") for i in range(50)))
        assert values == [str(i) for i in range(50)]  # each reply matched to its request


async def test_a_timeout_fails_every_request_on_the_connection() -> None:
    release = asyncio.Event()

    async def slow(command: str, *args: str) -> str:
        await release.wait()
        return "late"

    server = TCPServer(slow, host="127.0.0.1", port=0)
    await server.start()
    try:
        client = KVClient("127.0.0.1", server.port, timeout_s=0.2)
        first = asyncio.create_task(client.execute("PING"))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(client.execute("PING"))
        with pytest.raises(NodeUnavailableError, match="TimeoutError"):
            await first
        with pytest.raises(NodeUnavailableError):
            await second  # its connection was dropped with the first one
        release.set()
        assert await client.execute("PING") == "late"  # a new connection, a fresh reply
        await client.close()
    finally:
        release.set()
        await server.stop()


async def test_unsolicited_reply_is_a_protocol_error() -> None:
    async def chatty(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(100)
        writer.write(b"+OK\r\n+EXTRA\r\n")
        await writer.drain()
        await reader.read(100)  # until the client hangs up
        writer.close()

    server = await asyncio.start_server(chatty, "127.0.0.1", 0)
    async with server, KVClient("127.0.0.1", server.sockets[0].getsockname()[1]) as client:
        assert await client.execute("PING") == "OK"
        await asyncio.sleep(0.05)
        assert not client.connected  # the extra reply poisoned the connection


async def test_connect_failure_is_distinguishable() -> None:
    async with KVClient("127.0.0.1", 1, timeout_s=0.5) as client:
        with pytest.raises(ConnectFailedError, match=r"127.0.0.1:1 unavailable"):
            await client.execute("PING")


async def test_pool_spreads_requests(servers: list[CountingServer]) -> None:
    server = await start_server(servers)
    pool = KVClientPool(server.address, size=3)
    try:
        for _ in range(6):
            assert await pool.execute("PING") == "PONG"
        assert server.server.connections_received == 3
        assert await pool.pipeline([["SET", "a", "1"], ["GET", "a"]]) == ["OK", "1"]
    finally:
        await pool.close()
    with pytest.raises(ValueError, match="at least 1"):
        KVClientPool(server.address, size=0)


# ----------------------------------------------------------------- router
async def test_a_pipeline_through_the_router_stays_a_pipeline(
    servers: list[CountingServer],
) -> None:
    shards = [await start_server(servers) for _ in range(3)]
    router = ShardRouter([s.address for s in shards])
    try:
        commands = [["SET", f"k{i}", str(i)] for i in range(300)]
        commands += [["GET", f"k{i}"] for i in range(300)]
        results = await router.execute_batch(commands)
        assert results[:300] == ["OK"] * 300
        assert results[300:] == [str(i) for i in range(300)]
        # One round trip per shard, not per command.
        assert all(s.reads == 1 for s in shards), [s.reads for s in shards]
        # Fan-out is a barrier: the writes before it are counted.
        assert await router.execute_batch([["SET", "x", "1"], ["DBSIZE"]]) == ["OK", 301]
    finally:
        await router.close()


async def test_router_retries_until_a_restarted_node_is_back(
    servers: list[CountingServer],
) -> None:
    shard = await start_server(servers)
    port = shard.server.port
    await shard.server.stop()
    # A short connect timeout: on Windows, connecting to a closed loopback
    # port doesn't fail fast (the SYN is retried for ~2 s).
    router = ShardRouter([f"127.0.0.1:{port}"], timeout_s=0.3, retries=4, retry_backoff_s=0.05)

    async def restart() -> None:
        await asyncio.sleep(0.1)
        shard.server = TCPServer(shard._handle, host="127.0.0.1", port=port)
        await shard.server.start()

    try:
        restarting = asyncio.create_task(restart())
        assert await router.execute("SET", "a", "1") == "OK"  # never sent until it was up
        await restarting
        assert router.retries_performed >= 1
    finally:
        await router.close()


@pytest.mark.parametrize("error", [ReadOnlyReplicaError, TryAgainError])
async def test_router_retries_commands_the_node_refused(
    servers: list[CountingServer], error: type[KVStoreError]
) -> None:
    refusals = {"n": 2}

    def flaky(server: CountingServer, *command: str) -> Any:
        if refusals["n"]:
            refusals["n"] -= 1
            raise error()
        return server.engine.execute(*command)

    shard = await start_server(servers, flaky)
    stale_calls = []

    async def on_stale() -> None:
        stale_calls.append(1)

    router = ShardRouter([shard.address], retry_backoff_s=0.01, on_stale=on_stale)
    try:
        assert await router.execute("SET", "a", "1") == "OK"
        assert len(shard.calls) == 3
        assert bool(stale_calls) == (error is ReadOnlyReplicaError)
    finally:
        await router.close()


async def test_router_follows_ask_redirects(servers: list[CountingServer]) -> None:
    target = await start_server(servers)

    def moved(server: CountingServer, *command: str) -> Any:
        raise AskRedirectError(f"shard-2 {target.address}")

    source = await start_server(servers, moved)
    router = ShardRouter([source.address], retry_backoff_s=0.01)
    try:
        assert await router.execute("SET", "a", "1") == "OK"
        assert target.engine.execute("GET", "a") == "1"
        assert await router.execute("GET", "a") == "1"
        assert len(source.calls) == 2  # asked once per command, then redirected
    finally:
        await router.close()


async def test_router_does_not_repeat_a_write_whose_outcome_is_unknown() -> None:
    calls = []

    async def hang(command: str, *args: str) -> str:
        calls.append(command)
        await asyncio.sleep(1)
        return "OK"

    server = TCPServer(hang, host="127.0.0.1", port=0)
    await server.start()
    router = ShardRouter([f"127.0.0.1:{server.port}"], timeout_s=0.2, retry_backoff_s=0.01)
    try:
        with pytest.raises(NodeUnavailableError):
            await router.execute("SET", "a", "1")
        assert calls == ["SET"]  # not re-sent: it may have been applied
        with pytest.raises(NodeUnavailableError):
            await router.execute("GET", "a")
        assert calls.count("GET") == 4  # reads are safe to repeat: 1 + 3 retries
    finally:
        await router.close()
        await server.stop()


async def test_router_applies_only_newer_configs(servers: list[CountingServer]) -> None:
    old, new = await start_server(servers), await start_server(servers)
    config = ClusterConfig.from_spec([f"s={old.address}"])
    router = ShardRouter(config)
    try:
        assert await router.execute("SET", "a", "old") == "OK"
        failed_over = config.with_group(ShardGroup("s", new.address))
        assert router.apply_config(failed_over)
        assert not router.apply_config(config)  # stale epoch ignored
        assert await router.execute("SET", "a", "new") == "OK"
        assert new.engine.execute("GET", "a") == "new"
        assert old.engine.execute("GET", "a") == "old"
        assert router.primary("s") == new.address
    finally:
        await router.close()


# ------------------------------------------------------------ group commit
async def test_concurrent_clients_share_one_fsync(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, aof_fsync="always")
    async with running_app(settings) as (app, _):
        engine: Engine = app.state.engine
        port = app.state.tcp_server.port
        clients = [KVClient("127.0.0.1", port) for _ in range(20)]
        try:
            await asyncio.gather(*(c.execute("PING") for c in clients))  # connect all
            stats = engine.info().persistence
            assert stats is not None
            before = stats.aof_fsyncs
            for round_ in range(5):
                replies = await asyncio.gather(
                    *(c.execute("SET", f"k{i}", str(round_)) for i, c in enumerate(clients))
                )
                assert replies == ["OK"] * 20
            stats = engine.info().persistence
            assert stats is not None
            # 100 acknowledged writes from 20 connections: far fewer than 100 fsyncs.
            assert stats.aof_fsyncs - before <= 30
        finally:
            for c in clients:
                await c.close()


async def test_http_writes_wait_for_the_group_commit(tmp_path: Path) -> None:
    async with running_app(make_settings(tmp_path, aof_fsync="always")) as (app, http):
        group: GroupCommit = app.state.group_commit
        commits = group.commits
        assert (await http.put("/v1/keys/a", json={"value": "1"})).status_code == 200
        assert group.commits > commits  # the reply waited for a commit


async def test_failed_shared_commit_fails_every_waiting_connection() -> None:
    committed: list[int] = []

    def commit() -> None:
        committed.append(1)
        raise KVStoreError("disk gone")

    group = GroupCommit(lambda: None, commit)
    engine = Engine()
    server = TCPServer(engine.execute, host="127.0.0.1", port=0, group_commit=group)
    await server.start()
    try:
        results = await asyncio.gather(
            *(raw(server.port, encode_command(["SET", f"k{i}", "v"])) for i in range(5))
        )
        assert results == [b""] * 5  # nobody was told their write succeeded
        assert committed
    finally:
        await server.stop()


async def raw(port: int, payload: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(payload)
    data = await asyncio.wait_for(reader.read(100), 5)
    writer.close()
    return data
