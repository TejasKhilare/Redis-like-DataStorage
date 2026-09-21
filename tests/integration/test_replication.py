"""Primary-replica replication between real nodes over TCP."""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from kvstore.core.exceptions import (
    CommandError,
    NotEnoughReplicasError,
    ReadOnlyReplicaError,
)
from kvstore.engine import Engine
from kvstore.protocol.client import KVClient
from kvstore.replication.node import ShardNode
from tests.helpers import dump, make_settings, running_app


class Node:
    def __init__(self, app: Any) -> None:
        self.app = app

    @property
    def node(self) -> ShardNode:
        node: ShardNode = self.app.state.node
        return node

    @property
    def engine(self) -> Engine:
        engine: Engine = self.app.state.engine
        return engine

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.app.state.tcp_server.port}"

    def client(self) -> KVClient:
        return KVClient.from_address(self.address, timeout_s=5)

    def run(self, *command: Any) -> Any:
        return self.node.execute(*command)


@asynccontextmanager
async def start_node(tmp_path: Path, name: str, **settings: Any) -> AsyncIterator[Node]:
    values = {"data_dir": tmp_path / name, "repl_ping_interval_s": 0.1, **settings}
    async with running_app(make_settings(tmp_path, **values)) as (app, _):
        yield Node(app)


async def eventually(check: Callable[[], bool | Awaitable[bool]], within_s: float = 5.0) -> None:
    deadline = time.monotonic() + within_s
    while True:
        result = check()
        if not isinstance(result, bool):
            result = await result
        if result:
            return
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def in_sync(primary: Node, *replicas: Node) -> None:
    """Wait until every replica has applied the primary's whole stream."""

    def done() -> bool:
        offset = primary.node.state.offset
        return all(
            r.node.link is not None and r.node.link.up and r.node.state.offset == offset
            for r in replicas
        )

    await eventually(done)


@pytest.fixture
async def pair(tmp_path: Path) -> AsyncIterator[tuple[Node, Node]]:
    async with AsyncExitStack() as stack:
        primary = await stack.enter_async_context(start_node(tmp_path, "primary"))
        replica = await stack.enter_async_context(
            start_node(tmp_path, "replica", replicaof=primary.address)
        )
        await in_sync(primary, replica)
        yield primary, replica


def populate(node: Node) -> None:
    node.run("SET", "s", "v")
    node.run("SET", "ttl", "x", "EX", "1000")
    node.run("RPUSH", "l", "a", "b", "c")
    node.run("HSET", "h", "f1", "1", "f2", "2")
    node.run("SADD", "set", "x", "y", "z")
    node.run("ZADD", "z", "1", "one", "2", "two")
    node.run("INCRBY", "n", "41")


async def test_full_sync_then_streaming(tmp_path: Path) -> None:
    async with start_node(tmp_path, "primary") as primary:
        populate(primary)  # before the replica exists: it must arrive via the snapshot
        async with start_node(tmp_path, "replica", replicaof=primary.address) as replica:
            await in_sync(primary, replica)
            assert dump(replica.engine) == dump(primary.engine)
            assert primary.node.primary.full_resyncs == 1

            # ...then every kind of write, as its effect.
            primary.run("INCR", "n")
            primary.run("LPOP", "l")
            primary.run("SPOP", "set")  # random: replicated as the SREM it did
            primary.run("EXPIRE", "s", "500")  # replicated as an absolute PEXPIREAT
            primary.run("ZINCRBY", "z", "5", "one")
            primary.run("HDEL", "h", "f1")
            primary.run("DEL", "ttl")
            await in_sync(primary, replica)
            assert dump(replica.engine) == dump(primary.engine)
            assert replica.run("GET", "n") == "42"


async def test_replica_is_read_only_and_reports_its_role(pair: tuple[Node, Node]) -> None:
    primary, replica = pair
    primary.run("SET", "k", "v")
    await in_sync(primary, replica)
    assert replica.run("GET", "k") == "v"
    with pytest.raises(ReadOnlyReplicaError):
        replica.run("SET", "k", "other")
    async with replica.client() as client:  # over RESP, as -READONLY
        with pytest.raises(ReadOnlyReplicaError):
            await client.execute("DEL", "k")

    role, offset, replicas = primary.run("ROLE")
    assert role == "master" and offset == primary.node.state.offset
    assert replicas[0][1] == str(replica.app.state.tcp_server.port)
    assert replica.run("ROLE")[:2] == ["slave", "127.0.0.1"]
    assert replica.run("ROLE")[3] == "connected"

    info = replica.run("INFO", "replication")
    assert "role:slave" in info and "master_link_status:up" in info
    assert "connected_slaves:1" in primary.run("INFO", "replication")


async def test_http_writes_to_a_replica_are_refused(tmp_path: Path) -> None:
    async with start_node(tmp_path, "primary") as primary:
        settings = make_settings(tmp_path, data_dir=tmp_path / "r", replicaof=primary.address)
        async with running_app(settings) as (_, http):
            response = await http.put("/v1/keys/a", json={"value": "1"})
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "READ_ONLY_REPLICA"


async def test_reconnect_continues_from_the_backlog(pair: tuple[Node, Node]) -> None:
    primary, replica = pair
    primary.run("SET", "a", "1")
    await in_sync(primary, replica)
    primary.node.primary.disconnect_all()  # the link breaks
    primary.run("SET", "b", "2")  # written while the replica is away
    await eventually(lambda: primary.node.primary.partial_resyncs == 1)
    await in_sync(primary, replica)
    assert replica.run("GET", "b") == "2"
    assert primary.node.primary.full_resyncs == 1  # only the initial one


async def test_reconnect_after_backlog_overflow_needs_a_full_resync(tmp_path: Path) -> None:
    async with AsyncExitStack() as stack:
        primary = await stack.enter_async_context(
            start_node(tmp_path, "primary", repl_backlog_bytes=256)
        )
        replica = await stack.enter_async_context(
            start_node(tmp_path, "replica", replicaof=primary.address)
        )
        await in_sync(primary, replica)
        replica.node.link.cancel()  # type: ignore[union-attr]
        primary.node.primary.disconnect_all()
        for i in range(50):  # far more than 256 bytes of stream
            primary.run("SET", f"k{i}", "x" * 20)
        replica.node.link.start()  # type: ignore[union-attr]
        await eventually(lambda: primary.node.primary.full_resyncs == 2)
        await in_sync(primary, replica)
        assert dump(replica.engine) == dump(primary.engine)


async def test_expiry_reaches_replicas_as_deletes(pair: tuple[Node, Node]) -> None:
    primary, replica = pair
    primary.run("SET", "short", "x", "PX", "150")
    await in_sync(primary, replica)
    await asyncio.sleep(0.3)
    # Expired on the replica's clock: invisible, but not deleted by the replica.
    assert replica.run("GET", "short") is None
    assert replica.engine.store.peek("short") is None
    await eventually(lambda: "short" not in replica.engine.store._data)  # the primary's DEL
    assert primary.engine.info().expired_keys == 1
    assert replica.engine.info().expired_keys == 0


async def test_wait_counts_acknowledging_replicas(pair: tuple[Node, Node]) -> None:
    primary, replica = pair
    async with primary.client() as client:
        assert await client.execute("SET", "k", "v") == "OK"
        assert await client.execute("WAIT", "1", "2000") == 1
        started = time.monotonic()
        assert await client.execute("WAIT", "2", "200") == 1  # only one replica exists
        assert time.monotonic() - started >= 0.15
    with pytest.raises(CommandError, match="replica"):
        await replica.run("WAIT", "1", "10")


async def test_min_replicas_to_write(tmp_path: Path) -> None:
    async with start_node(tmp_path, "primary", min_replicas_to_write=1) as primary:
        with pytest.raises(NotEnoughReplicasError):
            primary.run("SET", "a", "1")
        assert primary.run("GET", "a") is None  # reads are unaffected
        async with start_node(tmp_path, "replica", replicaof=primary.address) as replica:
            await in_sync(primary, replica)
            await eventually(lambda: bool(primary.node.primary.replicas))
            assert primary.run("SET", "a", "1") == "OK"


async def test_promotion_and_rejoining_without_divergence(pair: tuple[Node, Node]) -> None:
    primary, replica = pair
    primary.run("SET", "a", "1")
    await in_sync(primary, replica)

    assert replica.run("REPLICAOF", "NO", "ONE", "EPOCH", "1") == "OK"
    assert replica.node.role == "primary"
    assert replica.run("SET", "b", "2") == "OK"  # writable now
    assert replica.node.epoch == 1

    # The old primary rejoins as a replica: same history, so a partial resync.
    host, port = replica.address.split(":")
    assert primary.run("REPLICAOF", host, port, "EPOCH", "1") == "OK"
    await in_sync(replica, primary)
    assert replica.node.primary.partial_resyncs == 1
    assert primary.run("GET", "b") == "2"
    with pytest.raises(ReadOnlyReplicaError):
        primary.run("SET", "c", "3")


async def test_divergent_writes_are_discarded_on_rejoin(pair: tuple[Node, Node]) -> None:
    primary, replica = pair
    primary.run("SET", "a", "1")
    await in_sync(primary, replica)
    replica.run("REPLICAOF", "NO", "ONE", "EPOCH", "1")
    # The old primary didn't hear about it and took a write only it has
    # (the write a failover loses with asynchronous replication).
    primary.run("SET", "lost", "x")
    replica.run("SET", "b", "2")

    host, port = replica.address.split(":")
    primary.run("REPLICAOF", host, port, "EPOCH", "1")
    await in_sync(replica, primary)
    assert replica.node.primary.full_resyncs == 1  # its history diverged: start over
    assert primary.run("GET", "lost") is None
    assert dump(primary.engine) == dump(replica.engine)


async def test_stale_epochs_are_refused_and_epochs_survive_restarts(tmp_path: Path) -> None:
    async with start_node(tmp_path, "n") as node:
        assert node.run("REPLICAOF", "NO", "ONE", "EPOCH", "5") == "OK"
        with pytest.raises(CommandError, match="stale epoch 3"):
            node.run("REPLICAOF", "127.0.0.1", "1", "EPOCH", "3")
        assert node.node.role == "primary"
        with pytest.raises(CommandError, match="wrong number"):
            node.run("REPLICAOF", "x")
    async with start_node(tmp_path, "n") as node:
        assert node.node.epoch == 5
        assert "epoch:5" in node.run("INFO", "replication")


async def test_a_replica_persists_what_it_received(tmp_path: Path) -> None:
    async with start_node(tmp_path, "primary") as primary:
        populate(primary)
        async with start_node(tmp_path, "replica", replicaof=primary.address) as replica:
            await in_sync(primary, replica)
            primary.run("SET", "after", "sync")
            await in_sync(primary, replica)
            expected = dump(replica.engine)
    # The replica restarts alone, as a primary, from its own files.
    async with start_node(tmp_path, "replica") as restarted:
        assert dump(restarted.engine) == expected
