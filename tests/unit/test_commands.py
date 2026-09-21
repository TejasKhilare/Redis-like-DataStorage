"""Command semantics through the public Engine.execute API (Redis behaviour as the reference)."""

import random
import threading
from typing import Any

import pytest

from kvstore.core.exceptions import (
    CommandError,
    InvalidArgumentError,
    OutOfMemoryError,
    UnknownCommandError,
    WrongArityError,
    WrongTypeError,
)
from kvstore.engine import Engine
from kvstore.engine.commands import COMMANDS, DENYOOM, STATELESS, WRITE
from kvstore.protocol.resp import SimpleString
from tests.helpers import FakeClock


def run(engine: Engine, *args: Any) -> Any:
    return engine.execute(*args)


# ---------------------------------------------------------------- strings
def test_set_get(engine: Engine) -> None:
    assert run(engine, "SET", "a", "1") == "OK"
    assert isinstance(run(engine, "SET", "a", "2"), SimpleString)
    assert run(engine, "GET", "a") == "2"
    assert run(engine, "GET", "missing") is None


def test_set_options(engine: Engine, clock: FakeClock) -> None:
    assert run(engine, "SET", "a", "1", "NX") == "OK"
    assert run(engine, "SET", "a", "2", "NX") is None
    assert run(engine, "SET", "b", "1", "XX") is None
    assert run(engine, "SET", "a", "3", "XX", "GET") == "1"
    assert run(engine, "SET", "a", "4", "px", "1500") == "OK"
    assert run(engine, "PTTL", "a") == 1500
    assert run(engine, "SET", "a", "5", "KEEPTTL") == "OK"
    assert run(engine, "PTTL", "a") == 1500
    assert run(engine, "SET", "a", "6") == "OK"
    assert run(engine, "TTL", "a") == -1
    assert run(engine, "SETNX", "a", "x") == 0
    assert run(engine, "SETNX", "new", "x") == 1


@pytest.mark.parametrize(
    "args",
    [
        ("SET", "a", "1", "NX", "XX"),
        ("SET", "a", "1", "EX"),
        ("SET", "a", "1", "EX", "5", "PX", "5"),
        ("SET", "a", "1", "KEEPTTL", "EX", "5"),
        ("SET", "a", "1", "BOGUS"),
    ],
)
def test_set_syntax_errors(engine: Engine, args: tuple[str, ...]) -> None:
    with pytest.raises(InvalidArgumentError, match="syntax error"):
        run(engine, *args)


def test_set_rejects_bad_expire(engine: Engine) -> None:
    with pytest.raises(InvalidArgumentError, match="invalid expire time"):
        run(engine, "SET", "a", "1", "EX", "0")


def test_counters(engine: Engine) -> None:
    assert run(engine, "INCR", "n") == 1
    assert run(engine, "INCRBY", "n", "10") == 11
    assert run(engine, "DECR", "n") == 10
    assert run(engine, "DECRBY", "n", "4") == 6
    assert run(engine, "INCRBYFLOAT", "f", "1.5") == "1.5"
    assert run(engine, "INCRBYFLOAT", "f", "-0.5") == "1"
    run(engine, "SET", "s", "abc")
    with pytest.raises(InvalidArgumentError, match="not an integer"):
        run(engine, "INCR", "s")
    with pytest.raises(InvalidArgumentError, match="not a valid float"):
        run(engine, "INCRBYFLOAT", "s", "1")
    run(engine, "SET", "big", str(2**63 - 1))
    with pytest.raises(InvalidArgumentError, match="overflow"):
        run(engine, "INCR", "big")


def test_counters_keep_ttl(engine: Engine) -> None:
    run(engine, "SET", "n", "1", "EX", "100")
    run(engine, "INCR", "n")
    run(engine, "APPEND", "n", "0")
    assert run(engine, "TTL", "n") == 100
    assert run(engine, "GET", "n") == "20"


def test_append_strlen_getdel(engine: Engine) -> None:
    assert run(engine, "APPEND", "s", "héllo") == 6  # byte length, like Redis
    assert run(engine, "STRLEN", "s") == 6
    assert run(engine, "STRLEN", "missing") == 0
    assert run(engine, "GETDEL", "s") == "héllo"
    assert run(engine, "GETDEL", "s") is None


def test_mset_mget(engine: Engine) -> None:
    assert run(engine, "MSET", "a", "1", "b", "2") == "OK"
    run(engine, "RPUSH", "l", "x")
    assert run(engine, "MGET", "a", "b", "missing", "l") == ["1", "2", None, None]
    with pytest.raises(WrongArityError):
        run(engine, "MSET", "a", "1", "b")


# ------------------------------------------------------------------ lists
def test_lists(engine: Engine) -> None:
    assert run(engine, "RPUSH", "l", "a", "b", "c") == 3
    assert run(engine, "LPUSH", "l", "z") == 4
    assert run(engine, "LRANGE", "l", "0", "-1") == ["z", "a", "b", "c"]
    assert run(engine, "LINDEX", "l", "-1") == "c"
    assert run(engine, "LLEN", "l") == 4
    assert run(engine, "LSET", "l", "0", "Z") == "OK"
    assert run(engine, "LPOP", "l") == "Z"
    assert run(engine, "RPOP", "l", "2") == ["c", "b"]
    assert run(engine, "LPOP", "missing") is None
    assert run(engine, "LPOP", "missing", "2") is None
    with pytest.raises(InvalidArgumentError, match="no such key"):
        run(engine, "LSET", "missing", "0", "x")
    with pytest.raises(InvalidArgumentError, match="index out of range"):
        run(engine, "LSET", "l", "9", "x")
    with pytest.raises(InvalidArgumentError, match="must be positive"):
        run(engine, "LPOP", "l", "-1")


def test_ltrim_lrem(engine: Engine) -> None:
    run(engine, "RPUSH", "l", "a", "b", "a", "c", "a")
    assert run(engine, "LREM", "l", "2", "a") == 2
    assert run(engine, "LRANGE", "l", "0", "-1") == ["b", "c", "a"]
    assert run(engine, "LTRIM", "l", "1", "-1") == "OK"
    assert run(engine, "LRANGE", "l", "0", "-1") == ["c", "a"]
    assert run(engine, "LTRIM", "missing", "0", "1") == "OK"
    assert run(engine, "LREM", "missing", "0", "a") == 0


def test_popping_the_last_element_deletes_the_key(engine: Engine) -> None:
    run(engine, "RPUSH", "l", "only")
    run(engine, "LPOP", "l")
    assert run(engine, "EXISTS", "l") == 0
    assert run(engine, "TYPE", "l") == "none"


# ----------------------------------------------------------------- hashes
def test_hashes(engine: Engine) -> None:
    assert run(engine, "HSET", "h", "a", "1", "b", "2") == 2
    assert run(engine, "HSET", "h", "a", "10") == 0
    assert run(engine, "HGET", "h", "a") == "10"
    assert run(engine, "HMGET", "h", "a", "zz") == ["10", None]
    assert run(engine, "HGETALL", "h") == ["a", "10", "b", "2"]
    assert run(engine, "HKEYS", "h") == ["a", "b"]
    assert run(engine, "HVALS", "h") == ["10", "2"]
    assert run(engine, "HLEN", "h") == 2
    assert run(engine, "HEXISTS", "h", "a") == 1
    assert run(engine, "HSETNX", "h", "a", "x") == 0
    assert run(engine, "HSETNX", "h", "c", "3") == 1
    assert run(engine, "HINCRBY", "h", "a", "5") == 15
    assert run(engine, "HINCRBY", "h", "new", "-1") == -1
    assert run(engine, "HDEL", "h", "a", "zz") == 1
    assert run(engine, "HGETALL", "missing") == []
    assert run(engine, "HMGET", "missing", "a") == [None]
    run(engine, "HSET", "h", "s", "x")
    with pytest.raises(InvalidArgumentError, match="not an integer"):
        run(engine, "HINCRBY", "h", "s", "1")
    with pytest.raises(WrongArityError):
        run(engine, "HSET", "h", "a", "1", "b")


# ------------------------------------------------------------------- sets
def test_sets(engine: Engine) -> None:
    assert run(engine, "SADD", "s", "a", "b", "c", "a") == 3
    assert run(engine, "SREM", "s", "c", "zz") == 1
    assert sorted(run(engine, "SMEMBERS", "s")) == ["a", "b"]
    assert run(engine, "SISMEMBER", "s", "a") == 1
    assert run(engine, "SCARD", "s") == 2
    assert run(engine, "SRANDMEMBER", "s") in {"a", "b"}
    assert sorted(run(engine, "SRANDMEMBER", "s", "5")) == ["a", "b"]
    assert len(run(engine, "SRANDMEMBER", "s", "-5")) == 5
    run(engine, "SADD", "t", "b", "c")
    assert run(engine, "SINTER", "s", "t") == ["b"]
    assert sorted(run(engine, "SUNION", "s", "t")) == ["a", "b", "c"]
    assert run(engine, "SDIFF", "s", "t") == ["a"]
    assert run(engine, "SINTER", "s", "missing") == []
    popped = run(engine, "SPOP", "s", "5")
    assert sorted(popped) == ["a", "b"]
    assert run(engine, "EXISTS", "s") == 0
    assert run(engine, "SPOP", "s") is None
    assert run(engine, "SPOP", "s", "2") == []
    assert run(engine, "SRANDMEMBER", "s") is None


# ------------------------------------------------------------ sorted sets
def test_zadd_and_ranges(engine: Engine) -> None:
    assert run(engine, "ZADD", "z", "1", "a", "2", "b", "3", "c") == 3
    assert run(engine, "ZRANGE", "z", "0", "-1") == ["a", "b", "c"]
    assert run(engine, "ZRANGE", "z", "0", "-1", "WITHSCORES") == ["a", "1", "b", "2", "c", "3"]
    assert run(engine, "ZREVRANGE", "z", "0", "0", "WITHSCORES") == ["c", "3"]
    assert run(engine, "ZRANGE", "z", "0", "-1", "REV") == ["c", "b", "a"]
    assert run(engine, "ZRANGEBYSCORE", "z", "(1", "+inf") == ["b", "c"]
    assert run(engine, "ZRANGEBYSCORE", "z", "-inf", "+inf", "LIMIT", "1", "1") == ["b"]
    assert run(engine, "ZREVRANGEBYSCORE", "z", "3", "(1") == ["c", "b"]
    assert run(engine, "ZRANGE", "z", "(1", "3", "BYSCORE") == ["b", "c"]
    assert run(engine, "ZRANGE", "z", "3", "1", "BYSCORE", "REV", "LIMIT", "0", "2") == ["c", "b"]
    assert run(engine, "ZCOUNT", "z", "2", "+inf") == 2
    assert run(engine, "ZRANK", "z", "c") == 2
    assert run(engine, "ZREVRANK", "z", "c") == 0
    assert run(engine, "ZRANK", "z", "zz") is None
    assert run(engine, "ZSCORE", "z", "b") == "2"
    assert run(engine, "ZCARD", "z") == 3


def test_zadd_options(engine: Engine) -> None:
    run(engine, "ZADD", "z", "5", "a")
    assert run(engine, "ZADD", "z", "NX", "1", "a", "1", "b") == 1
    assert run(engine, "ZSCORE", "z", "a") == "5"
    assert run(engine, "ZADD", "z", "XX", "CH", "7", "a", "1", "c") == 1
    assert run(engine, "ZADD", "z", "GT", "CH", "6", "a", "9", "b") == 1  # a: 7 -> 6 refused
    assert run(engine, "ZSCORE", "z", "b") == "9"
    assert run(engine, "ZADD", "z", "LT", "10", "a") == 0
    assert run(engine, "ZADD", "z", "INCR", "2.5", "a") == "9.5"
    assert run(engine, "ZADD", "z", "XX", "INCR", "1", "missing") is None
    assert run(engine, "ZADD", "nokey", "XX", "1", "a") == 0
    assert run(engine, "ZINCRBY", "z", "-0.5", "a") == "9"
    assert run(engine, "ZADD", "z", "1", "inf-test", "+inf", "top") == 2
    assert run(engine, "ZSCORE", "z", "top") == "inf"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("ZADD", "z", "NX", "XX", "1", "a"), "not compatible"),
        (("ZADD", "z", "GT", "LT", "1", "a"), "not compatible"),
        (("ZADD", "z", "INCR", "1", "a", "2", "b"), "single increment"),
        (("ZADD", "z", "1", "a", "2"), "syntax error"),
        (("ZADD", "z", "nan", "a"), "not a valid float"),
        (("ZADD", "z", "abc", "a"), "not a valid float"),
        (("ZRANGEBYSCORE", "z", "x", "1"), "min or max is not a float"),
        (("ZRANGE", "z", "0", "1", "LIMIT", "0", "1"), "syntax error"),
        (("ZRANGEBYSCORE", "z", "0", "1", "BYSCORE"), "syntax error"),
        (("ZPOPMIN", "z", "-1"), "must be positive"),
    ],
)
def test_zset_errors(engine: Engine, args: tuple[str, ...], message: str) -> None:
    with pytest.raises(InvalidArgumentError, match=message):
        run(engine, *args)


def test_zrem_and_pop(engine: Engine) -> None:
    run(engine, "ZADD", "z", "1", "a", "2", "b", "3", "c")
    assert run(engine, "ZREM", "z", "a", "zz") == 1
    assert run(engine, "ZPOPMAX", "z") == ["c", "3"]
    assert run(engine, "ZPOPMIN", "z", "5") == ["b", "2"]
    assert run(engine, "EXISTS", "z") == 0
    assert run(engine, "ZPOPMIN", "z") == []
    assert run(engine, "ZREM", "z", "a") == 0
    assert run(engine, "ZRANGE", "missing", "0", "-1") == []
    assert run(engine, "ZCOUNT", "missing", "0", "1") == 0


# ---------------------------------------------------------------- keyspace
def test_type_and_wrongtype(engine: Engine) -> None:
    run(engine, "SET", "s", "x")
    run(engine, "LPUSH", "l", "x")
    run(engine, "HSET", "h", "f", "v")
    run(engine, "SADD", "st", "m")
    run(engine, "ZADD", "z", "1", "m")
    assert [run(engine, "TYPE", k) for k in ["s", "l", "h", "st", "z", "no"]] == [
        "string", "list", "hash", "set", "zset", "none"
    ]  # fmt: skip
    wrong_type_commands = [
        ("GET", "l"),
        ("LPUSH", "s", "x"),
        ("HGET", "z", "f"),
        ("SADD", "h", "m"),
        ("ZADD", "st", "1", "m"),
        ("INCR", "l"),
        ("ZINCRBY", "s", "1", "m"),
    ]
    for command in wrong_type_commands:
        with pytest.raises(WrongTypeError):
            run(engine, *command)


def test_del_exists_keys_dbsize_flush(engine: Engine) -> None:
    run(engine, "MSET", "user:1", "a", "user:2", "b", "other", "c")
    assert run(engine, "EXISTS", "user:1", "user:1", "nope") == 2
    assert sorted(run(engine, "KEYS", "user:*")) == ["user:1", "user:2"]
    assert run(engine, "DEL", "user:1", "nope") == 1
    assert run(engine, "UNLINK", "user:2") == 1
    assert run(engine, "DBSIZE") == 1
    assert run(engine, "FLUSHALL") == "OK"
    assert run(engine, "DBSIZE") == 0
    assert run(engine, "FLUSHDB", "ASYNC") == "OK"
    with pytest.raises(InvalidArgumentError):
        run(engine, "FLUSHALL", "LATER")


def test_expiry_commands(engine: Engine, clock: FakeClock) -> None:
    assert run(engine, "TTL", "missing") == -2
    run(engine, "SET", "a", "1")
    assert run(engine, "TTL", "a") == -1
    assert run(engine, "EXPIRE", "a", "10") == 1
    assert run(engine, "TTL", "a") == 10
    assert run(engine, "PEXPIRE", "a", "2500") == 1
    assert run(engine, "PTTL", "a") == 2500
    assert run(engine, "EXPIREAT", "a", str(int(clock.now) + 100)) == 1
    assert run(engine, "TTL", "a") == 100
    assert run(engine, "PERSIST", "a") == 1
    assert run(engine, "PERSIST", "a") == 0
    assert run(engine, "PEXPIREAT", "a", str(int(clock.now * 1000) + 1500)) == 1
    clock.advance(9.4)
    assert run(engine, "GET", "a") is None
    assert run(engine, "EXPIRE", "missing", "10") == 0
    run(engine, "SET", "b", "1")
    assert run(engine, "EXPIRE", "b", "0") == 1  # deadline in the past: deleted now
    assert run(engine, "EXISTS", "b") == 0


def test_collections_can_expire(engine: Engine, clock: FakeClock) -> None:
    run(engine, "RPUSH", "l", "a")
    run(engine, "EXPIRE", "l", "5")
    run(engine, "RPUSH", "l", "b")  # writes keep the TTL
    assert run(engine, "TTL", "l") == 5
    clock.advance(6)
    assert run(engine, "LRANGE", "l", "0", "-1") == []


# ------------------------------------------------------------------ server
def test_connection_commands(engine: Engine) -> None:
    assert run(engine, "PING") == "PONG"
    assert run(engine, "PING", "hi") == "hi"
    assert run(engine, "ECHO", "x") == "x"
    assert run(engine, "SELECT", "0") == "OK"
    with pytest.raises(CommandError, match="out of range"):
        run(engine, "SELECT", "1")
    assert run(engine, "CLIENT", "SETINFO", "lib-name", "redis-py") == "OK"
    assert run(engine, "CLIENT", "GETNAME") is None
    assert run(engine, "CLIENT", "ID") == 1
    with pytest.raises(CommandError):
        run(engine, "CLIENT", "KILL")
    seconds, micros = run(engine, "TIME")
    assert int(seconds) > 1_600_000_000
    assert 0 <= int(micros) < 1_000_000
    assert run(engine, "HELLO")[:2] == ["server", "kvstore"]
    with pytest.raises(CommandError) as info:
        run(engine, "HELLO", "3")
    assert info.value.to_resp().startswith("NOPROTO")


def test_command_introspection(engine: Engine) -> None:
    assert run(engine, "COMMAND", "COUNT") == len(COMMANDS)
    assert run(engine, "COMMAND", "DOCS") == []
    assert "zadd" in run(engine, "COMMAND", "LIST")
    get_info, missing = run(engine, "COMMAND", "INFO", "get", "nope")
    assert get_info[:2] == ["get", 2]
    assert get_info[3:] == [1, 1, 1]
    assert missing is None
    del_info = next(i for i in run(engine, "COMMAND") if i[0] == "del")
    assert del_info[1] == -2
    assert del_info[4] == -1
    with pytest.raises(CommandError):
        run(engine, "COMMAND", "BOGUS")


def test_config_and_info(engine: Engine) -> None:
    assert run(engine, "CONFIG", "GET", "maxmemory*") == [
        "maxmemory", "0", "maxmemory-policy", "allkeys-lru"
    ]  # fmt: skip
    assert run(engine, "CONFIG", "GET", "appendonly", "save") == ["appendonly", "no", "save", ""]
    assert run(engine, "CONFIG", "RESETSTAT") == "OK"
    with pytest.raises(CommandError, match="unsupported"):
        run(engine, "CONFIG", "SET", "maxmemory", "1")
    run(engine, "SET", "a", "1")
    text = run(engine, "INFO")
    assert "# Server" in text
    assert "db0:keys=1,expires=0" in text
    assert "aof_enabled:0" in text
    memory = run(engine, "INFO", "memory")
    assert memory.startswith("# Memory") and "# Stats" not in memory
    assert isinstance(run(engine, "LASTSAVE"), int)
    with pytest.raises(CommandError, match="persistence is disabled"):
        run(engine, "BGSAVE")


# ------------------------------------------------------- limits & eviction
def test_maxmemory_evicts_lru_and_logs(clock: FakeClock) -> None:
    engine = Engine(maxmemory=4000, clock=clock)
    for i in range(40):
        run(engine, "SET", f"k{i}", "x" * 100)
    info = engine.info()
    assert info.used_memory <= 4000
    assert info.evicted_keys > 0
    assert run(engine, "EXISTS", "k39") == 1  # the key just written is never the victim
    assert run(engine, "EXISTS", "k0") == 0


def test_noeviction_refuses_writes_that_add_data(clock: FakeClock) -> None:
    engine = Engine(max_keys=2, eviction_policy="noeviction", clock=clock)
    run(engine, "SET", "a", "1")
    run(engine, "SET", "b", "1")
    with pytest.raises(OutOfMemoryError) as info:
        run(engine, "SET", "c", "1")
    assert info.value.to_resp().startswith("OOM")
    assert run(engine, "SET", "a", "2") == "OK"  # overwriting adds no key: allowed
    assert run(engine, "DEL", "a") == 1  # DEL is not denyoom
    assert run(engine, "SET", "c", "1") == "OK"


def test_noeviction_with_maxmemory(clock: FakeClock) -> None:
    engine = Engine(maxmemory=500, eviction_policy="noeviction", clock=clock)
    run(engine, "SET", "big", "x" * 1000)  # admitted: usage was under the limit
    with pytest.raises(OutOfMemoryError):
        run(engine, "RPUSH", "l", "x")
    assert run(engine, "GET", "big") is not None


def test_lfu_policy_through_the_engine(clock: FakeClock) -> None:
    engine = Engine(max_keys=10, eviction_policy="lfu", clock=clock, rng=random.Random(3))
    for i in range(10):
        run(engine, "SET", f"hot{i}", "x")
    for _ in range(30):
        for i in range(10):
            run(engine, "GET", f"hot{i}")
    for i in range(30):
        run(engine, "SET", f"cold{i}", "x")
    survivors = set(run(engine, "KEYS", "hot*"))
    assert len(survivors) >= 8  # sampled LFU is approximate, but hot keys survive


# ------------------------------------------------------------------ errors
def test_unknown_and_arity(engine: Engine) -> None:
    with pytest.raises(UnknownCommandError, match="unknown command 'NOPE'"):
        run(engine, "NOPE")
    with pytest.raises(UnknownCommandError):
        engine.execute(123)  # type: ignore[arg-type]
    with pytest.raises(WrongArityError, match="'get' command"):
        run(engine, "GET")
    with pytest.raises(WrongArityError):
        run(engine, "DBSIZE", "x")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("SET", 1, "v"), "key must be a string"),
        (("SET", "a", None), "value must be a string"),
        (("SET", "a", 5), "value must be a string"),
        (("EXPIRE", "a", "1.5"), "not an integer"),
        (("EXPIRE", "a", True), "not an integer"),
        (("INCRBY", "a", str(2**64)), "not an integer"),
        (("LRANGE", "l", "a", "1"), "not an integer"),
    ],
)
def test_invalid_arguments(engine: Engine, args: tuple[Any, ...], message: str) -> None:
    with pytest.raises(InvalidArgumentError, match=message):
        run(engine, *args)


def test_failed_command_leaves_no_empty_collection(engine: Engine) -> None:
    with pytest.raises(InvalidArgumentError):
        run(engine, "ZADD", "z", "1", "a", "oops", "b")
    assert run(engine, "EXISTS", "z") == 0
    assert engine.info().used_memory == 0


# ----------------------------------------------------------- command table
def test_command_table_flags() -> None:
    assert WRITE in COMMANDS["SET"].flags and DENYOOM in COMMANDS["SET"].flags
    assert DENYOOM not in COMMANDS["DEL"].flags
    assert STATELESS in COMMANDS["PING"].flags
    assert COMMANDS["MSET"].keys(["a", "1", "b", "2"]) == ["a", "b"]
    assert COMMANDS["DEL"].keys(["a", "b"]) == ["a", "b"]
    assert COMMANDS["DBSIZE"].keys([]) == []
    assert COMMANDS["GET"].arity == 2
    assert COMMANDS["SET"].arity == -3


def test_concurrent_writers_are_serialized(clock: FakeClock) -> None:
    engine = Engine(clock=clock)

    def writer(prefix: str) -> None:
        for i in range(2000):
            engine.execute("INCR", "counter")
            engine.execute("SET", f"{prefix}{i}", "x")

    threads = [threading.Thread(target=writer, args=(f"t{n}-",)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert engine.execute("GET", "counter") == "8000"
    assert engine.execute("DBSIZE") == 8001
