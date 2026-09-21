"""AOF, snapshots, manifest-based rewrites, fsync policies and crash recovery."""

import contextlib
import json
import random
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest

from kvstore.core.exceptions import (
    AOFCorruptedError,
    CommandError,
    InvalidArgumentError,
    PersistenceError,
    PersistenceWriteError,
    SnapshotCorruptedError,
)
from kvstore.engine import Engine
from kvstore.engine.persistence import aof as aof_module
from kvstore.engine.persistence import manager as manager_module
from kvstore.engine.persistence.aof import decode_record, encode_record
from kvstore.engine.persistence.manifest import Manifest
from tests.helpers import FakeClock, dump


def open_engine(data_dir: Path, clock: FakeClock, **kwargs: Any) -> Engine:
    kwargs.setdefault("aof_fsync", "no")
    engine = Engine(data_dir=data_dir, clock=clock, rng=random.Random(0), **kwargs)
    engine.open()
    return engine


def aof_records(data_dir: Path) -> list[list[Any]]:
    manifest = Manifest.load(data_dir)
    assert manifest is not None
    records = []
    for name in manifest.aofs:
        for line in (data_dir / name).read_bytes().splitlines(keepends=True):
            command, args = decode_record(line)
            records.append([command, *args])
    return records


# ------------------------------------------------------------ record format
def test_record_round_trip_with_crc() -> None:
    line = encode_record("SET", ["k", "v with ünicode"])
    assert line[8:9] == b" " and line.endswith(b"\n")
    assert decode_record(line) == ("SET", ["k", "v with ünicode"])


@pytest.mark.parametrize(
    ("line", "message"),
    [
        (b'deadbeef ["SET","k","v"]\n', "checksum mismatch"),
        (b'zzzzzzzz ["SET"]\n', "malformed checksum"),
        (b"garbage\n", "malformed record"),
        (b'{"command": 5, "args": []}\n', "needs a string"),
        (b"[1]\n", "malformed"),
    ],
)
def test_bad_records(line: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        decode_record(line)


def test_crc_must_cover_a_valid_array() -> None:
    payload = b'{"not":"an array"}'
    import zlib

    with pytest.raises(ValueError, match="non-empty array"):
        decode_record(b"%08x %s\n" % (zlib.crc32(payload), payload))


# ------------------------------------------------------------- basic AOF
def test_state_survives_restart(durable_engine: Engine, data_dir: Path, clock: FakeClock) -> None:
    run = durable_engine.execute
    run("SET", "s", "v")
    run("RPUSH", "l", "a", "b", "c")
    run("LPOP", "l")
    run("HSET", "h", "f", "1")
    run("HINCRBY", "h", "f", "9")
    run("SADD", "st", "x", "y")
    run("ZADD", "z", "1", "a", "2", "b")
    run("ZINCRBY", "z", "5", "a")
    run("INCRBYFLOAT", "f", "0.1")
    run("SET", "gone", "x")
    run("DEL", "gone")
    before = dump(durable_engine)
    durable_engine.close()

    engine = open_engine(data_dir, clock)
    assert dump(engine) == before
    engine.close()


def test_only_effects_are_logged(durable_engine: Engine, data_dir: Path, clock: FakeClock) -> None:
    run = durable_engine.execute
    run("SET", "a", "1")
    with pytest.raises(InvalidArgumentError):
        run("SET", "a", "1", "EX", "0")
    run("DEL", "missing")  # no-op
    run("SREM", "missing", "m")  # no-op
    run("GET", "a")  # read
    run("EXPIRE", "a", "60")
    run("SADD", "s", "x", "y")
    popped = run("SPOP", "s")
    run("INCRBYFLOAT", "f", "1.5")
    run("ZADD", "z", "INCR", "2", "m")
    records = aof_records(data_dir)
    deadline = int(clock.now * 1000) + 60_000
    assert records == [
        ["SET", "a", "1"],
        ["PEXPIREAT", "a", deadline],
        ["SADD", "s", "x", "y"],
        ["SREM", "s", popped],  # SPOP is random: logged as the member removed
        ["SET", "f", "1.5", "KEEPTTL"],
        ["ZADD", "z", "2", "m"],
    ]


def test_ttl_is_absolute_across_restarts(
    durable_engine: Engine, data_dir: Path, clock: FakeClock
) -> None:
    durable_engine.execute("SET", "a", "1", "EX", "60")
    durable_engine.execute("SET", "gone", "1", "EX", "5")
    durable_engine.close()
    clock.advance(20)
    engine = open_engine(data_dir, clock)
    assert engine.execute("TTL", "a") == 40
    assert engine.execute("EXISTS", "gone") == 0
    engine.close()


def test_persist_after_expire_survives_late_restart(
    durable_engine: Engine, data_dir: Path, clock: FakeClock
) -> None:
    """Regression (phase 1): replay used to expire keys before a later PERSIST."""
    durable_engine.execute("SET", "a", "1", "EX", "5")
    durable_engine.execute("PERSIST", "a")
    durable_engine.close()
    clock.advance(60)
    engine = open_engine(data_dir, clock)
    assert engine.execute("GET", "a") == "1"
    engine.close()


def test_evictions_are_logged_and_replayed_exactly(data_dir: Path, clock: FakeClock) -> None:
    engine = open_engine(data_dir, clock, max_keys=3)
    for key in ("k1", "k2", "k3"):
        engine.execute("SET", key, key)
    engine.execute("GET", "k1")
    engine.execute("SET", "k4", "k4")
    engine.close()
    assert aof_records(data_dir)[-2:] == [["SET", "k4", "k4"], ["DEL", "k2"]]

    engine = open_engine(data_dir, clock, max_keys=3)
    assert sorted(engine.execute("KEYS", "*")) == ["k1", "k3", "k4"]
    engine.close()


def test_lowering_limits_trims_on_startup(data_dir: Path, clock: FakeClock) -> None:
    engine = open_engine(data_dir, clock)
    for i in range(5):
        engine.execute("SET", f"k{i}", "x")
    engine.close()
    engine = open_engine(data_dir, clock, max_keys=2)
    assert engine.execute("DBSIZE") == 2
    engine.close()


# ------------------------------------------------------ corruption & tails
def test_torn_tail_is_truncated(durable_engine: Engine, data_dir: Path, clock: FakeClock) -> None:
    durable_engine.execute("SET", "a", "1")
    durable_engine.close()
    aof = data_dir / "appendonly-1.aof"
    good_size = aof.stat().st_size
    with aof.open("ab") as f:
        f.write(encode_record("SET", ["b", "2"])[:-5])  # crash mid-write

    engine = open_engine(data_dir, clock)
    assert engine.execute("EXISTS", "a", "b") == 1
    stats = engine.info().persistence
    assert stats is not None and stats.aof_truncated_bytes > 0
    assert aof.stat().st_size == good_size
    engine.execute("SET", "c", "3")
    engine.close()
    assert aof_records(data_dir)[-1] == ["SET", "c", "3"]


def test_bitflip_in_the_middle_is_detected(
    durable_engine: Engine, data_dir: Path, clock: FakeClock
) -> None:
    for i in range(3):
        durable_engine.execute("SET", f"k{i}", "value")
    durable_engine.close()
    aof = data_dir / "appendonly-1.aof"
    data = bytearray(aof.read_bytes())
    data[data.index(b"k1") + 1] ^= 0x01  # one flipped bit, still valid JSON
    aof.write_bytes(bytes(data))
    with pytest.raises(AOFCorruptedError, match=":2: checksum mismatch"):
        open_engine(data_dir, clock)


def test_unreplayable_command_aborts(data_dir: Path, clock: FakeClock) -> None:
    data_dir.mkdir(parents=True)
    (data_dir / "appendonly-1.aof").write_bytes(encode_record("FLY", []))
    with pytest.raises(AOFCorruptedError, match="cannot replay"):
        open_engine(data_dir, clock)


def test_blank_lines_are_ignored(data_dir: Path, clock: FakeClock) -> None:
    data_dir.mkdir(parents=True)
    (data_dir / "appendonly-1.aof").write_bytes(b"\n" + encode_record("SET", ["a", "1"]) + b"\n")
    engine = open_engine(data_dir, clock)
    assert engine.execute("GET", "a") == "1"
    engine.close()


def test_legacy_v1_directory_is_migrated(data_dir: Path, clock: FakeClock) -> None:
    """A phase-1 data dir: a single appendonly.aof of JSON objects, no manifest."""
    data_dir.mkdir(parents=True)
    legacy = [
        {"command": "SET", "args": ["user", {"id": 7, "name": "Tejas"}]},
        {"command": "SET", "args": ["n", 5]},
        {"command": "EXPIREAT", "args": ["n", int(clock.now) + 30]},
        {"command": "PEXPIREAT", "args": ["user", int(clock.now * 1000) + 90_000]},
    ]
    (data_dir / "appendonly.aof").write_text("".join(json.dumps(r) + "\n" for r in legacy))

    engine = open_engine(data_dir, clock)
    assert json.loads(engine.execute("GET", "user")) == {"id": 7, "name": "Tejas"}
    assert engine.execute("GET", "n") == "5"
    assert engine.execute("TTL", "n") == 30
    engine.execute("SET", "new", "v")  # appended in the v2 format to the same file
    engine.execute("BGREWRITEAOF")
    engine.close()
    assert not (data_dir / "appendonly.aof").exists()  # folded into the snapshot

    engine = open_engine(data_dir, clock)
    assert engine.execute("TTL", "user") == 90
    assert engine.execute("GET", "new") == "v"
    engine.close()


def test_unreadable_manifest(data_dir: Path, clock: FakeClock) -> None:
    data_dir.mkdir(parents=True)
    (data_dir / "manifest.json").write_text('{"snapshot": null, "aofs": []}')
    with pytest.raises(PersistenceError, match="at least one AOF"):
        open_engine(data_dir, clock)
    (data_dir / "manifest.json").write_text("not json")
    with pytest.raises(PersistenceError, match="unreadable"):
        open_engine(data_dir, clock)


def test_writes_require_open(data_dir: Path, clock: FakeClock) -> None:
    engine = Engine(data_dir=data_dir, clock=clock)
    assert engine.execute("GET", "a") is None
    with pytest.raises(PersistenceError, match="not open"):
        engine.execute("SET", "a", "1")


# --------------------------------------------------------------- rewrites
def test_rewrite_compacts_to_snapshot_plus_new_aof(
    durable_engine: Engine, data_dir: Path, clock: FakeClock
) -> None:
    run = durable_engine.execute
    for _ in range(200):
        run("INCR", "counter")
    run("ZADD", "z", "1.5", "a", "-inf", "b")
    run("RPUSH", "l", *[str(i) for i in range(50)])
    run("SET", "ttl", "x", "EX", "100")
    before = dump(durable_engine)

    assert run("BGREWRITEAOF") == "Background append only file rewriting started"
    with pytest.raises(CommandError, match="already in progress"):
        run("BGSAVE")
    assert durable_engine.persistence is not None
    durable_engine.persistence.wait_rewrite()
    run("SET", "after", "rewrite")
    stats = durable_engine.info().persistence
    assert stats is not None
    assert stats.rewrites_completed == 1 and stats.last_rewrite_status == "ok"
    assert stats.last_snapshot_pause_ms is not None and stats.aof_base_size > 0
    durable_engine.close()

    assert sorted(p.name for p in data_dir.iterdir()) == [
        "appendonly-2.aof",
        "manifest.json",
        "snapshot-2.snap",
    ]
    assert aof_records(data_dir) == [["SET", "after", "rewrite"]]

    engine = open_engine(data_dir, clock)
    before["after"] = ("str", "rewrite", None)
    assert dump(engine) == before
    stats = engine.info().persistence
    assert stats is not None and stats.snapshot_keys_loaded == 4
    engine.close()


def test_crash_during_rewrite_loses_nothing(
    data_dir: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Freeze the snapshot writer, keep writing, then 'crash' by copying the directory."""
    release = threading.Event()
    real_write = manager_module.write_snapshot

    def slow_write(*args: Any, **kwargs: Any) -> int:
        release.wait(10)
        return real_write(*args, **kwargs)

    monkeypatch.setattr(manager_module, "write_snapshot", slow_write)
    engine = open_engine(data_dir, clock)
    engine.execute("SET", "before", "1")
    engine.execute("BGREWRITEAOF")
    engine.execute("SET", "during", "2")
    engine.execute("DEL", "before")

    crashed = tmp_path / "crashed"
    shutil.copytree(data_dir, crashed)  # the disk as a crash would leave it
    release.set()
    engine.close()

    manifest = Manifest.load(crashed)
    assert manifest is not None and manifest.aofs == ["appendonly-1.aof", "appendonly-2.aof"]
    recovered = open_engine(crashed, clock)
    assert recovered.execute("KEYS", "*") == ["during"]
    recovered.close()


def test_failed_rewrite_keeps_every_file(
    data_dir: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(manager_module, "write_snapshot", broken)
    engine = open_engine(data_dir, clock)
    engine.execute("SET", "a", "1")
    engine.execute("BGREWRITEAOF")
    engine.execute("SET", "b", "2")
    assert engine.persistence is not None
    engine.persistence.wait_rewrite()
    stats = engine.info().persistence
    assert stats is not None
    assert (stats.last_rewrite_status, stats.rewrites_failed) == ("err", 1)
    engine.close()

    engine = open_engine(data_dir, clock)
    assert sorted(engine.execute("KEYS", "*")) == ["a", "b"]
    engine.close()


def test_save_blocks_until_done(durable_engine: Engine, data_dir: Path) -> None:
    durable_engine.execute("SET", "a", "1")
    assert durable_engine.execute("SAVE") == "OK"
    assert (data_dir / "snapshot-2.snap").exists()
    assert durable_engine.execute("LASTSAVE") > 0


def test_save_reports_failure(durable_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_module, "write_snapshot", lambda *a, **k: 1 / 0)
    with pytest.raises(PersistenceError, match="snapshot failed"):
        durable_engine.execute("SAVE")


def test_auto_rewrite_when_aof_grows(data_dir: Path, clock: FakeClock) -> None:
    engine = open_engine(data_dir, clock, aof_rewrite_min_bytes=2000, aof_rewrite_percentage=100)
    for i in range(100):
        engine.execute("SET", "k", str(i))  # 100 records, one live key
    engine.cron()  # starts the rewrite
    assert engine.persistence is not None
    engine.persistence.wait_rewrite()
    stats = engine.info().persistence
    assert stats is not None and stats.rewrites_completed == 1
    assert stats.aof_current_size == 0
    engine.cron()  # AOF is now tiny: no new rewrite
    assert not engine.persistence.rewrite_in_progress
    engine.close()


def test_corrupted_snapshot_aborts(
    durable_engine: Engine, data_dir: Path, clock: FakeClock
) -> None:
    durable_engine.execute("SET", "a", "1")
    durable_engine.execute("SAVE")
    durable_engine.close()
    snapshot = data_dir / "snapshot-2.snap"
    data = bytearray(snapshot.read_bytes())
    data[20] ^= 0xFF
    snapshot.write_bytes(bytes(data))
    with pytest.raises(SnapshotCorruptedError, match="checksum mismatch"):
        open_engine(data_dir, clock)
    snapshot.write_bytes(b"short")
    with pytest.raises(SnapshotCorruptedError, match="too short"):
        open_engine(data_dir, clock)


def test_stale_temp_files_are_cleaned_up(data_dir: Path, clock: FakeClock) -> None:
    engine = open_engine(data_dir, clock)
    engine.close()
    (data_dir / "snapshot-9.snap.tmp").write_bytes(b"half written")
    (data_dir / "appendonly-9.aof").write_bytes(b"orphan")
    engine = open_engine(data_dir, clock)
    engine.close()
    assert sorted(p.name for p in data_dir.iterdir()) == ["appendonly-1.aof", "manifest.json"]


def test_randomized_workload_survives_rewrites_and_restarts(
    data_dir: Path, clock: FakeClock
) -> None:
    """Random commands, random rewrites, random restarts: state must always match."""
    rng = random.Random(42)
    engine = open_engine(data_dir, clock)
    keys = [f"k{i}" for i in range(15)]
    commands = [
        lambda k: ("SET", k, str(rng.randrange(100))),
        lambda k: ("DEL", k),
        lambda k: ("RPUSH", k, str(rng.randrange(9))),
        lambda k: ("LPOP", k),
        lambda k: ("HSET", k, f"f{rng.randrange(3)}", "v"),
        lambda k: ("SADD", k, str(rng.randrange(5))),
        lambda k: ("SPOP", k),
        lambda k: ("ZADD", k, str(rng.randrange(-5, 5)), f"m{rng.randrange(4)}"),
        lambda k: ("ZPOPMIN", k),
        lambda k: ("EXPIRE", k, str(rng.randrange(1, 30))),
        lambda k: ("PERSIST", k),
        lambda k: ("INCR", k),
    ]
    for step in range(1500):
        make = rng.choice(commands)
        with contextlib.suppress(CommandError):  # WRONGTYPE etc.: fine, and never logged
            engine.execute(*make(rng.choice(keys)))
        clock.advance(rng.random() * 0.05)
        if step % 250 == 100:
            with contextlib.suppress(CommandError):  # the previous one may still be running
                engine.execute("BGREWRITEAOF")
        if step % 300 == 299:
            before = dump(engine)
            engine.close()
            engine = open_engine(data_dir, clock)
            assert dump(engine) == before
    engine.close()


# ------------------------------------------------------------ fsync policies
def test_fsync_always_syncs_every_commit(data_dir: Path, clock: FakeClock) -> None:
    engine = open_engine(data_dir, clock, aof_fsync="always")
    for i in range(5):
        engine.execute("SET", f"k{i}", "v")
    engine.execute("GET", "k0")  # reads don't sync
    stats = engine.info().persistence
    assert stats is not None and stats.aof_fsyncs == 5
    engine.close()


def test_group_commit_shares_one_fsync(data_dir: Path, clock: FakeClock) -> None:
    engine = open_engine(data_dir, clock, aof_fsync="always")
    with engine.deferred_commit():
        for i in range(100):
            engine.execute("SET", f"k{i}", "v")
        with engine.deferred_commit():  # nesting is fine
            engine.execute("SET", "inner", "v")
    stats = engine.info().persistence
    assert stats is not None and stats.aof_fsyncs == 1
    engine.close()


def test_fsync_no_leaves_it_to_the_os(data_dir: Path, clock: FakeClock) -> None:
    engine = open_engine(data_dir, clock, aof_fsync="no")
    engine.execute("SET", "a", "1")
    stats = engine.info().persistence
    assert stats is not None and stats.aof_fsyncs == 0
    engine.close()


def test_fsync_everysec_runs_in_the_background(data_dir: Path, clock: FakeClock) -> None:
    engine = open_engine(data_dir, clock, aof_fsync="everysec")
    engine.execute("SET", "a", "1")
    assert engine.persistence is not None
    writer = engine.persistence._writer
    assert writer is not None
    deadline = threading.Event()
    for _ in range(30):
        if writer.fsyncs:
            break
        deadline.wait(0.1)
    assert writer.fsyncs >= 1
    engine.close()


# --------------------------------------------------------- write failures
def test_aof_write_failure_refuses_further_writes(
    data_dir: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = open_engine(data_dir, clock)
    engine.execute("SET", "a", "1")

    def broken_commit(self: aof_module.AOFWriter) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(aof_module.AOFWriter, "commit", broken_commit)
    with pytest.raises(PersistenceWriteError, match="No space left"):
        engine.execute("SET", "b", "2")
    monkeypatch.undo()

    with pytest.raises(PersistenceWriteError) as info:
        engine.execute("SET", "c", "3")
    assert info.value.to_resp().startswith("MISCONF")
    assert engine.execute("GET", "a") == "1"  # reads still work
    with pytest.raises(PersistenceWriteError):
        engine.execute("BGREWRITEAOF")
    stats = engine.info().persistence
    assert stats is not None and stats.write_error
    assert "aof_last_write_status:err" in engine.execute("INFO", "persistence")
    engine.close()


def test_background_fsync_failure_surfaces_on_next_commit(data_dir: Path, clock: FakeClock) -> None:
    engine = open_engine(data_dir, clock)
    assert engine.persistence is not None and engine.persistence._writer is not None
    engine.persistence._writer.background_error = OSError("I/O error")
    with pytest.raises(PersistenceWriteError, match="I/O error"):
        engine.execute("SET", "a", "1")
    engine.close()
