"""Compatibility: the official redis-py client, unmodified, against a shard and the router."""

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
import redis

from tests.helpers import make_settings, running_app


async def in_thread(fn: Callable[[], Any]) -> Any:
    """redis-py is blocking; run it off the event loop that serves it."""
    return await asyncio.to_thread(fn)


@pytest.fixture
async def shard_port(tmp_path: Path) -> AsyncIterator[int]:
    async with running_app(make_settings(tmp_path)) as (app, _):
        yield app.state.tcp_server.port


def client(port: int) -> redis.Redis:
    return redis.Redis(port=port, protocol=2, decode_responses=True, socket_timeout=5)


async def test_data_types(shard_port: int) -> None:
    def scenario() -> None:
        r = client(shard_port)
        assert r.ping() is True
        assert r.set("s", "v", ex=100) is True
        assert r.get("s") == "v"
        assert 99 <= r.ttl("s") <= 100
        assert r.incr("n") == 1
        assert r.incrbyfloat("f", 0.5) == 0.5
        assert r.mset({"a": "1", "b": "2"}) is True
        assert r.mget("a", "b", "zz") == ["1", "2", None]

        assert r.rpush("l", "a", "b", "c") == 3
        assert r.lrange("l", 0, -1) == ["a", "b", "c"]
        assert r.lpop("l", 2) == ["a", "b"]

        assert r.hset("h", mapping={"x": "1", "y": "2"}) == 2
        assert r.hgetall("h") == {"x": "1", "y": "2"}
        assert r.hincrby("h", "x", 5) == 6

        assert r.sadd("st", "a", "b") == 2
        assert r.smembers("st") == {"a", "b"}
        assert r.sismember("st", "a") == 1

        assert r.zadd("z", {"alice": 3, "bob": 1.5}) == 2
        assert r.zrange("z", 0, -1, withscores=True) == [("bob", 1.5), ("alice", 3.0)]
        assert r.zincrby("z", 10, "bob") == 11.5
        assert r.zrevrank("z", "bob") == 0
        assert r.zrangebyscore("z", "(3", "+inf") == ["bob"]
        assert r.zscore("z", "missing") is None

        assert r.type("z") == "zset"
        assert r.exists("s", "l", "nope") == 2
        assert r.delete("s") == 1
        assert r.expire("h", 10)
        assert r.persist("h")
        assert set(r.keys("*")) >= {"n", "h", "z"}

    await in_thread(scenario)


async def test_errors_become_response_errors(shard_port: int) -> None:
    def scenario() -> None:
        r = client(shard_port)
        r.set("s", "v")
        with pytest.raises(redis.ResponseError, match="WRONGTYPE"):
            r.lpush("s", "x")
        with pytest.raises(redis.ResponseError, match="unknown command"):
            r.execute_command("FLY")
        with pytest.raises(redis.ResponseError, match="wrong number of arguments"):
            r.execute_command("GET")
        assert r.get("s") == "v"  # the connection is still usable

    await in_thread(scenario)


async def test_pipelines(shard_port: int) -> None:
    def scenario() -> None:
        r = client(shard_port)
        pipe = r.pipeline(transaction=False)
        for i in range(500):
            pipe.set(f"k{i}", i)
        pipe.get("k499")
        pipe.lpush("k0", "x")  # an error in the middle doesn't break the rest
        pipe.dbsize()
        results = pipe.execute(raise_on_error=False)
        assert results[:500] == [True] * 500
        assert results[500] == "499"
        assert isinstance(results[501], redis.ResponseError)
        assert results[502] == 500

    await in_thread(scenario)


async def test_server_commands(shard_port: int) -> None:
    def scenario() -> None:
        r = client(shard_port)
        info = r.info()
        assert info["redis_version"] == "7.2.0"
        assert info["aof_enabled"] == 1
        assert r.config_get("appendfsync") == {"appendfsync": "no"}
        assert r.echo("hi") == "hi"
        assert r.dbsize() == 0
        assert r.bgrewriteaof() is True
        assert isinstance(r.lastsave(), object)
        r.set("x", "1")
        assert r.flushall() is True
        assert r.dbsize() == 0

    await in_thread(scenario)


async def test_through_the_router(tmp_path: Path) -> None:
    shard_dirs = [tmp_path / f"s{i}" for i in range(2)]
    async with (
        running_app(make_settings(shard_dirs[0])) as (s1, _),
        running_app(make_settings(shard_dirs[1])) as (s2, _),
    ):
        shards = ",".join(f"127.0.0.1:{s.state.tcp_server.port}" for s in (s1, s2))
        router_settings = make_settings(tmp_path / "r", node_role="router", shards=shards)
        async with running_app(router_settings) as (router, _):
            port = router.state.tcp_server.port
            # Ports are random, so pick two keys that certainly live on different shards.
            owner = router.state.router.owner
            split_a = "user:0"
            split_b = next(
                f"user:{i}" for i in range(1, 100) if owner(f"user:{i}") != owner(split_a)
            )

            def scenario() -> None:
                r = client(port)
                for i in range(50):
                    r.set(f"user:{i}", i)
                assert r.dbsize() == 50  # fanned out and summed
                assert len(r.keys("user:*")) == 50
                assert r.get("user:7") == "7"
                assert r.zadd("board", {"a": 1}) == 1
                # redis-py maps the CROSSSLOT prefix to its own exception type.
                with pytest.raises(redis.exceptions.ClusterCrossSlotError):
                    r.mset({split_a: "x", split_b: "y"})
                assert r.mset({"{cart:9}:items": "3", "{cart:9}:total": "42"}) is True
                assert r.mget("{cart:9}:items", "{cart:9}:total") == ["3", "42"]
                assert r.ping() is True
                assert r.flushall() is True
                assert r.dbsize() == 0

            await in_thread(scenario)
