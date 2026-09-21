"""AOF logging and crash recovery."""

import json
from pathlib import Path
from typing import Any

import pytest

from kvstore.core.exceptions import AOFCorruptedError, InvalidArgumentError, PersistenceError
from kvstore.engine import Engine
from tests.helpers import FakeClock


def read_log(path: Path) -> list[list[Any]]:
    return [
        [record["command"], *record["args"]]
        for record in map(json.loads, path.read_text().splitlines())
    ]


def reopen(path: Path, clock: FakeClock, max_keys: int = 100) -> Engine:
    engine = Engine(max_keys=max_keys, clock=clock, aof_path=path)
    engine.open()
    return engine


def test_state_survives_restart(durable_engine: Engine, aof_path: Path, clock: FakeClock) -> None:
    durable_engine.execute("SET", "user", {"id": 7, "name": "Tejas"})
    durable_engine.execute("SET", "counter", 1)
    durable_engine.execute("SET", "counter", 2)
    durable_engine.execute("SET", "gone", 1)
    durable_engine.execute("DEL", "gone")
    durable_engine.close()

    engine = reopen(aof_path, clock)

    assert engine.execute("GET", "user") == {"id": 7, "name": "Tejas"}
    assert engine.execute("GET", "counter") == 2
    assert engine.execute("EXISTS", "gone") == 0
    assert engine.info().aof_records_loaded == 5
    engine.close()


def test_only_successful_writes_are_logged(durable_engine: Engine, aof_path: Path) -> None:
    """Regression: invalid commands used to be written to the AOF before validation."""
    durable_engine.execute("SET", "a", 1)
    with pytest.raises(InvalidArgumentError):
        durable_engine.execute("SET", "a", None)
    durable_engine.execute("DEL", "missing")  # no-op: nothing to log
    durable_engine.execute("EXPIRE", "missing", 10)  # no-op
    durable_engine.execute("PERSIST", "a")  # no TTL: no-op
    durable_engine.execute("GET", "a")  # reads are never logged

    assert read_log(aof_path) == [["SET", "a", 1]]


def test_relative_ttl_is_logged_as_absolute_deadline(
    durable_engine: Engine, aof_path: Path, clock: FakeClock
) -> None:
    durable_engine.execute("SET", "a", 1)
    durable_engine.execute("EXPIRE", "a", 60)

    deadline_ms = int(clock.now * 1000) + 60_000
    assert read_log(aof_path)[-1] == ["PEXPIREAT", "a", deadline_ms]

    # Restart 20s later: 40s remain, not a fresh 60s.
    durable_engine.close()
    clock.advance(20)
    engine = reopen(aof_path, clock)
    assert engine.execute("TTL", "a") == 40
    engine.close()


def test_keys_that_expired_while_down_are_not_restored(
    durable_engine: Engine, aof_path: Path, clock: FakeClock
) -> None:
    durable_engine.execute("SET", "session", "x", "EX", 5)
    durable_engine.close()

    clock.advance(10)
    engine = reopen(aof_path, clock)
    assert engine.execute("EXISTS", "session") == 0
    engine.close()


def test_expire_to_past_is_logged_as_delete(durable_engine: Engine, aof_path: Path) -> None:
    durable_engine.execute("SET", "a", 1)
    durable_engine.execute("EXPIRE", "a", -1)
    assert read_log(aof_path)[-1] == ["DEL", "a"]


def test_persist_survives_restart(durable_engine: Engine, aof_path: Path, clock: FakeClock) -> None:
    durable_engine.execute("SET", "a", 1, "EX", 5)
    durable_engine.execute("PERSIST", "a")
    durable_engine.close()

    clock.advance(60)
    engine = reopen(aof_path, clock)
    assert engine.execute("GET", "a") == 1
    engine.close()


def test_evictions_are_logged_and_replayed_exactly(aof_path: Path, clock: FakeClock) -> None:
    """Reads aren't logged, so replaying with eviction on would evict the wrong key."""
    with Engine(max_keys=3, clock=clock, aof_path=aof_path) as engine:
        for key in ("k1", "k2", "k3"):
            engine.execute("SET", key, key)
        engine.execute("GET", "k1")  # k2 becomes the LRU victim
        engine.execute("SET", "k4", "k4")
    assert read_log(aof_path)[-2:] == [["SET", "k4", "k4"], ["DEL", "k2"]]

    engine = reopen(aof_path, clock, max_keys=3)
    assert engine.execute("EXISTS", "k1", "k3", "k4") == 3
    assert engine.execute("EXISTS", "k2") == 0
    engine.close()


def test_lowering_capacity_trims_on_startup(aof_path: Path, clock: FakeClock) -> None:
    with Engine(max_keys=10, clock=clock, aof_path=aof_path) as engine:
        for i in range(5):
            engine.execute("SET", f"k{i}", i)

    engine = reopen(aof_path, clock, max_keys=2)
    assert engine.execute("DBSIZE") == 2
    engine.close()
    assert read_log(aof_path)[-3:] == [["DEL", "k0"], ["DEL", "k1"], ["DEL", "k2"]]


def test_each_node_has_its_own_log(tmp_path: Path, clock: FakeClock) -> None:
    """Regression: every shard used to share ./appendonly.aof and load each other's keys."""
    with Engine(clock=clock, aof_path=tmp_path / "shard-1" / "appendonly.aof") as one:
        one.execute("SET", "only-on-1", 1)
    with Engine(clock=clock, aof_path=tmp_path / "shard-2" / "appendonly.aof") as two:
        assert two.execute("EXISTS", "only-on-1") == 0


def test_torn_tail_is_truncated(durable_engine: Engine, aof_path: Path, clock: FakeClock) -> None:
    durable_engine.execute("SET", "a", 1)
    durable_engine.close()
    good_size = aof_path.stat().st_size
    with aof_path.open("ab") as f:
        f.write(b'{"command":"SET","args":["b",')  # crash mid-write

    engine = reopen(aof_path, clock)
    assert engine.execute("GET", "a") == 1
    assert engine.execute("EXISTS", "b") == 0
    assert engine.info().aof_truncated_bytes > 0
    assert aof_path.stat().st_size == good_size

    # The log is appendable again after the cut.
    engine.execute("SET", "c", 3)
    engine.close()
    assert read_log(aof_path)[-1] == ["SET", "c", 3]


def test_complete_json_without_newline_counts_as_torn(aof_path: Path, clock: FakeClock) -> None:
    aof_path.write_bytes(b'{"command":"SET","args":["a",1]}\n{"command":"SET","args":["b",2]}')
    engine = reopen(aof_path, clock)
    assert engine.execute("EXISTS", "a", "b") == 1
    engine.close()


def test_corruption_in_the_middle_aborts(aof_path: Path, clock: FakeClock) -> None:
    aof_path.write_bytes(
        b'{"command":"SET","args":["a",1]}\ngarbage\n{"command":"SET","args":["b",2]}\n'
    )
    with pytest.raises(AOFCorruptedError, match=":2:"):
        reopen(aof_path, clock)


@pytest.mark.parametrize(
    "record",
    [b"[1, 2]\n", b'{"command": 5, "args": []}\n', b'{"command": "SET"}\n'],
)
def test_malformed_records_abort(aof_path: Path, clock: FakeClock, record: bytes) -> None:
    aof_path.write_bytes(record + b'{"command":"SET","args":["a",1]}\n')
    with pytest.raises(AOFCorruptedError):
        reopen(aof_path, clock)


def test_unreplayable_command_aborts(aof_path: Path, clock: FakeClock) -> None:
    aof_path.write_bytes(b'{"command":"FLY","args":[]}\n')
    with pytest.raises(AOFCorruptedError, match="cannot replay"):
        reopen(aof_path, clock)


def test_blank_lines_are_ignored(aof_path: Path, clock: FakeClock) -> None:
    aof_path.write_bytes(b'\n{"command":"SET","args":["a",1]}\n\n')
    engine = reopen(aof_path, clock)
    assert engine.execute("GET", "a") == 1
    assert engine.info().aof_truncated_bytes == 0
    engine.close()


def test_legacy_expireat_records_replay(aof_path: Path, clock: FakeClock) -> None:
    """Logs written by the pre-0.2 engine used EXPIREAT with unix seconds."""
    deadline = int(clock.now) + 30
    aof_path.write_text(
        json.dumps({"command": "SET", "args": ["a", 1]})
        + "\n"
        + json.dumps({"command": "EXPIREAT", "args": ["a", deadline]})
        + "\n"
    )
    engine = reopen(aof_path, clock)
    assert engine.execute("TTL", "a") == 30
    engine.close()


def test_writes_require_open(aof_path: Path, clock: FakeClock) -> None:
    engine = Engine(clock=clock, aof_path=aof_path)
    assert engine.execute("GET", "a") is None  # reads are fine
    with pytest.raises(PersistenceError, match="not open"):
        engine.execute("SET", "a", 1)


def test_open_and_close_are_idempotent(aof_path: Path, clock: FakeClock) -> None:
    engine = Engine(clock=clock, aof_path=aof_path)
    engine.open()
    engine.open()
    engine.execute("SET", "a", 1)
    info = engine.info()
    assert info.aof_enabled is True
    assert info.aof_size_bytes == aof_path.stat().st_size
    engine.close()
    engine.close()
