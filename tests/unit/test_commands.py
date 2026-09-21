"""Command semantics through the public Engine.execute API."""

import threading
from typing import Any

import pytest

from kvstore.core.exceptions import (
    InvalidArgumentError,
    UnknownCommandError,
    WrongArityError,
)
from kvstore.engine import Engine
from kvstore.engine.commands import COMMANDS
from tests.helpers import FakeClock


def test_ping(engine: Engine) -> None:
    assert engine.execute("PING") == "PONG"
    assert engine.execute("ping", "hello") == "hello"


def test_set_get_overwrite(engine: Engine) -> None:
    assert engine.execute("SET", "a", "1") == "OK"
    assert engine.execute("GET", "a") == "1"
    engine.execute("SET", "a", "2")
    assert engine.execute("GET", "a") == "2"


@pytest.mark.parametrize("value", [42, 3.5, True, "text", [1, "two"], {"name": "tejas", "age": 22}])
def test_values_can_be_any_json(engine: Engine, value: Any) -> None:
    engine.execute("SET", "k", value)
    assert engine.execute("GET", "k") == value


def test_get_missing_returns_none(engine: Engine) -> None:
    assert engine.execute("GET", "missing") is None


def test_del_and_exists_count_keys(engine: Engine) -> None:
    engine.execute("SET", "a", 1)
    engine.execute("SET", "b", 2)

    assert engine.execute("EXISTS", "a", "b", "c", "a") == 3  # duplicates count, like Redis
    assert engine.execute("DEL", "a", "c") == 1
    assert engine.execute("EXISTS", "a") == 0
    assert engine.execute("DBSIZE") == 1


def test_ttl_semantics(engine: Engine, clock: FakeClock) -> None:
    assert engine.execute("TTL", "missing") == -2
    engine.execute("SET", "a", 1)
    assert engine.execute("TTL", "a") == -1

    assert engine.execute("EXPIRE", "a", 10) == 1
    assert engine.execute("TTL", "a") == 10
    assert engine.execute("PTTL", "a") == 10_000

    clock.advance(9.4)
    assert engine.execute("TTL", "a") == 1  # rounds to nearest, like Redis
    clock.advance(1)
    assert engine.execute("GET", "a") is None
    assert engine.execute("TTL", "a") == -2


def test_set_with_ex(engine: Engine, clock: FakeClock) -> None:
    assert engine.execute("SET", "a", 1, "ex", 5) == "OK"
    assert engine.execute("TTL", "a") == 5
    clock.advance(5)
    assert engine.execute("EXISTS", "a") == 0


def test_set_clears_previous_ttl(engine: Engine) -> None:
    engine.execute("SET", "a", 1, "EX", 5)
    engine.execute("SET", "a", 2)
    assert engine.execute("TTL", "a") == -1


def test_expire_in_the_past_deletes(engine: Engine) -> None:
    engine.execute("SET", "a", 1)
    assert engine.execute("EXPIRE", "a", 0) == 1
    assert engine.execute("EXISTS", "a") == 0


def test_expire_missing_key(engine: Engine) -> None:
    assert engine.execute("EXPIRE", "missing", 10) == 0


def test_expireat_and_pexpireat(engine: Engine, clock: FakeClock) -> None:
    engine.execute("SET", "a", 1)
    engine.execute("SET", "b", 1)

    assert engine.execute("EXPIREAT", "a", int(clock.now) + 100) == 1
    assert engine.execute("PEXPIREAT", "b", int(clock.now * 1000) + 1500) == 1

    assert engine.execute("TTL", "a") == 100
    assert engine.execute("PTTL", "b") == 1500


def test_persist(engine: Engine, clock: FakeClock) -> None:
    engine.execute("SET", "a", 1, "EX", 5)
    assert engine.execute("PERSIST", "a") == 1
    assert engine.execute("PERSIST", "a") == 0
    clock.advance(10)
    assert engine.execute("GET", "a") == 1


def test_expire_accepts_numeric_strings(engine: Engine) -> None:
    engine.execute("SET", "a", 1)
    assert engine.execute("EXPIRE", "a", "30") == 1
    assert engine.execute("TTL", "a") == 30


def test_capacity_evicts_lru(clock: FakeClock) -> None:
    engine = Engine(max_keys=3, clock=clock)
    for key in ("k1", "k2", "k3"):
        engine.execute("SET", key, key)
    engine.execute("GET", "k1")
    engine.execute("SET", "k4", "k4")

    assert engine.execute("EXISTS", "k1", "k2", "k3", "k4") == 3
    assert engine.execute("GET", "k2") is None


# ------------------------------------------------------------------ errors
def test_unknown_command(engine: Engine) -> None:
    with pytest.raises(UnknownCommandError, match="unknown command 'NOPE'"):
        engine.execute("NOPE")


@pytest.mark.parametrize(
    "command",
    [("GET",), ("GET", "a", "b"), ("SET", "a"), ("DEL",), ("EXPIRE", "a"), ("DBSIZE", "x")],
)
def test_wrong_arity(engine: Engine, command: tuple[str, ...]) -> None:
    with pytest.raises(WrongArityError):
        engine.execute(*command)


@pytest.mark.parametrize(
    ("command", "message"),
    [
        (("SET", 1, "v"), "key must be a string"),
        (("SET", "a", None), "value must not be null"),
        (("SET", "a", 1, "PX", 5), "syntax error"),
        (("SET", "a", 1, "EX"), "syntax error"),
        (("SET", "a", 1, "EX", 0), "invalid expire time"),
        (("SET", "a", 1, "EX", "soon"), "not an integer"),
        (("EXPIRE", "a", 1.5), "not an integer"),
        (("EXPIRE", "a", True), "not an integer"),
        (("GET", ["a"]), "key must be a string"),
    ],
)
def test_invalid_arguments(engine: Engine, command: tuple[Any, ...], message: str) -> None:
    with pytest.raises(InvalidArgumentError, match=message):
        engine.execute(*command)


def test_failed_command_does_not_mutate(engine: Engine) -> None:
    engine.execute("SET", "a", 1)
    with pytest.raises(InvalidArgumentError):
        engine.execute("SET", "a", 2, "EX", -1)
    assert engine.execute("GET", "a") == 1


# ------------------------------------------------------------ command table
def test_key_specs_for_routing() -> None:
    assert COMMANDS["GET"].keys(["a"]) == ["a"]
    assert COMMANDS["SET"].keys(["a", 1, "EX", 10]) == ["a"]
    assert COMMANDS["DEL"].keys(["a", "b", "c"]) == ["a", "b", "c"]
    assert COMMANDS["PING"].keys([]) == []


def test_every_write_command_is_flagged() -> None:
    writes = {name for name, spec in COMMANDS.items() if spec.write}
    assert writes == {"SET", "DEL", "EXPIRE", "PEXPIREAT", "EXPIREAT", "PERSIST"}


# ------------------------------------------------------------------- misc
def test_info_counts(engine: Engine, clock: FakeClock) -> None:
    engine.execute("SET", "a", 1, "EX", 1)
    engine.execute("SET", "b", 1)
    clock.advance(2)
    assert engine.run_expiry_cycle() == 1

    info = engine.info()
    assert info.keys == 1
    assert info.expired_keys == 1
    assert info.commands_processed == 2
    assert info.aof_enabled is False
    assert info.aof_size_bytes is None


def test_concurrent_writers_are_serialized(clock: FakeClock) -> None:
    engine = Engine(max_keys=100_000, clock=clock)

    def writer(prefix: str) -> None:
        for i in range(2000):
            engine.execute("SET", f"{prefix}{i}", i)

    threads = [threading.Thread(target=writer, args=(f"t{n}-",)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert engine.execute("DBSIZE") == 8000
    assert engine.info().commands_processed == 8001
