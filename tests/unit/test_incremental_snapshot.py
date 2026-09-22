"""Incremental snapshots: copied in slices, yet exactly the keyspace as it was at the start."""

import random
from pathlib import Path
from typing import Any

import pytest

from kvstore.core.codec import SnapshotRecord
from kvstore.engine import Engine
from tests.helpers import FakeClock, dump


def as_set(records: list[SnapshotRecord]) -> dict[str, tuple[Any, ...]]:
    """Records keyed by name, collections made comparable."""
    out = {}
    for key, kind, payload, expires_at in records:
        value = sorted(payload) if kind in ("set", "hash") else payload
        out[key] = (kind, value if not isinstance(value, list) else tuple(map(str, value)),
                    expires_at)  # fmt: skip
    return out


def take(engine: Engine, between: Any = None) -> list[SnapshotRecord]:
    result: list[list[SnapshotRecord]] = []
    engine.begin_snapshot(result.append)
    while engine.step_snapshot():
        if between is not None:
            between()
    assert result, "the snapshot never finished"
    return result[0]


def test_changes_after_the_start_are_not_in_the_snapshot(clock: FakeClock) -> None:
    engine = Engine(clock=clock)
    engine.snapshot_slice_keys = 2
    engine.snapshot_slice_ms = 0  # one chunk per step
    for i in range(10):
        engine.execute("SET", f"k{i}", "old")
    expected = as_set(engine.store.snapshot())

    changes = iter(
        [
            ("SET", "k9", "new"),  # not copied yet: its old value must be kept
            ("DEL", "k8"),  # deleted before being copied
            ("SET", "fresh", "x"),  # created after the start: not in the snapshot
            ("APPEND", "k0", "!"),  # already copied: unaffected either way
            ("SET", "k8", "reborn"),  # re-created after deletion
        ]
    )

    def change() -> None:
        command = next(changes, None)
        if command:
            engine.execute(*command)

    records = take(engine, change)
    assert as_set(records) == expected
    assert engine.execute("GET", "k9") == "new"  # the live keyspace moved on


def test_random_writes_during_the_copy(clock: FakeClock) -> None:
    rng = random.Random(7)
    engine = Engine(clock=clock, rng=random.Random(1))
    engine.snapshot_slice_keys = 7
    engine.snapshot_slice_ms = 0  # one chunk per step
    for i in range(300):
        kind = i % 5
        key = f"k{i}"
        if kind == 0:
            engine.execute("SET", key, str(i))
        elif kind == 1:
            engine.execute("RPUSH", key, "a", "b", "c")
        elif kind == 2:
            engine.execute("HSET", key, "f", "1", "g", "2")
        elif kind == 3:
            engine.execute("SADD", key, "x", "y")
        else:
            engine.execute("ZADD", key, "1", "m", "2", "n")
        if i % 7 == 0:
            engine.execute("EXPIRE", key, "1000")
    expected = as_set(engine.store.snapshot())

    def mutate() -> None:
        for _ in range(5):
            i = rng.randrange(400)
            key = f"k{i}"
            kind = engine.execute("TYPE", key)
            op = rng.random()
            if op < 0.2:
                engine.execute("DEL", key)
            elif kind == "string" or kind == "none":
                engine.execute("SET", key, f"new{i}")
            elif kind == "list":
                engine.execute("LPUSH", key, "z")
            elif kind == "hash":
                engine.execute("HSET", key, "f", "changed")
            elif kind == "set":
                engine.execute("SREM", key, "x")
            else:
                engine.execute("ZINCRBY", key, "5", "m")

    assert as_set(take(engine, mutate)) == expected


def test_expiry_and_eviction_during_the_copy_keep_the_old_value(clock: FakeClock) -> None:
    engine = Engine(clock=clock, max_keys=50)
    engine.snapshot_slice_keys = 3
    engine.snapshot_slice_ms = 0  # one chunk per step
    for i in range(50):
        engine.execute("SET", f"k{i}", "v", "EX", "10" if i < 10 else "1000")
    expected = as_set(engine.store.snapshot())

    def expire_and_evict() -> None:
        clock.advance(20)  # k0..k9 expire: removed on the next access
        engine.execute("GET", "k0")
        engine.cron()
        engine.execute("SET", f"extra{clock.now}", "v")  # over max_keys: something is evicted

    assert as_set(take(engine, expire_and_evict)) == expected


def test_flushall_during_the_copy(clock: FakeClock) -> None:
    engine = Engine(clock=clock)
    engine.snapshot_slice_keys = 2
    engine.snapshot_slice_ms = 0  # one chunk per step
    for i in range(20):
        engine.execute("SET", f"k{i}", "v")
    expected = as_set(engine.store.snapshot())
    flushed = []

    def flush() -> None:
        if not flushed:
            # Deletions first: fewer keys left than keys still pending in the copy.
            engine.execute("DEL", "k0", "k1", "k2", "k3", "k4", "k5")
            flushed.append(engine.execute("FLUSHALL"))

    assert as_set(take(engine, flush)) == expected
    assert engine.execute("DBSIZE") == 0


def test_only_one_copy_at_a_time(clock: FakeClock) -> None:
    engine = Engine(clock=clock)
    engine.execute("SET", "a", "1")
    engine.begin_snapshot(lambda records: None)
    assert engine.snapshot_in_progress
    with pytest.raises(RuntimeError, match="already"):
        engine.store.begin_snapshot()
    while engine.step_snapshot():
        pass
    assert not engine.snapshot_in_progress
    assert not engine.step_snapshot()


def test_incremental_rewrite_recovers_exactly(data_dir: Path, clock: FakeClock) -> None:
    """Writes made while the copy runs land in the new AOF: snapshot + AOF = the final state."""
    with Engine(clock=clock, data_dir=data_dir, aof_fsync="no") as engine:
        engine.incremental_snapshots = True
        engine.snapshot_slice_keys = 10
        engine.snapshot_slice_ms = 0  # one chunk per step
        for i in range(200):
            engine.execute("SET", f"k{i}", "before")
        engine.execute("RPUSH", "list", "a")
        engine.start_rewrite()
        assert engine.snapshot_in_progress
        persistence = engine.persistence
        assert persistence is not None and persistence.rewrite_in_progress
        step = 0
        while engine.step_snapshot():
            engine.execute("SET", f"k{step}", "during")
            engine.execute("SET", f"new{step}", "x")
            engine.execute("RPUSH", "list", str(step))
            step += 1
        persistence.wait_rewrite()
        engine.execute("SET", "after", "rewrite")
        stats = persistence.stats()
        assert stats.rewrites_completed == 1
        assert stats.last_snapshot_pause_ms is not None
        expected = dump(engine)
    with Engine(clock=clock, data_dir=data_dir, aof_fsync="no") as restarted:
        assert dump(restarted) == expected


def test_save_finishes_the_copy_itself(data_dir: Path, clock: FakeClock) -> None:
    with Engine(clock=clock, data_dir=data_dir, aof_fsync="no") as engine:
        engine.incremental_snapshots = True
        for i in range(30):
            engine.execute("SET", f"k{i}", "v")
        engine.save()  # blocking by definition: no driver needed
        assert not engine.snapshot_in_progress
        persistence = engine.persistence
        assert persistence is not None and persistence.stats().rewrites_completed == 1


def test_info_reports_a_rewrite_while_its_copy_runs(data_dir: Path, clock: FakeClock) -> None:
    """Pollers wait on aof_rewrite_in_progress: it must cover the copy, not just the write."""
    engine = Engine(clock=clock, data_dir=data_dir, aof_fsync="no")
    engine.open()
    engine.incremental_snapshots = True
    engine.snapshot_slice_keys = 10
    engine.snapshot_slice_ms = 0  # one chunk per step
    for i in range(100):
        engine.execute("SET", f"k{i}", "v")
    engine.start_rewrite()
    assert engine.step_snapshot()  # still copying: nothing handed to the writer yet
    assert "aof_rewrite_in_progress:1" in engine.execute("INFO", "persistence")
    while engine.step_snapshot():
        pass
    persistence = engine.persistence
    assert persistence is not None
    persistence.wait_rewrite()
    assert "aof_rewrite_in_progress:0" in engine.execute("INFO", "persistence")
    engine.close()
