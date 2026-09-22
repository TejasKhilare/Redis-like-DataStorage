"""The GC hold that keeps full collections out of snapshot copies, and GC pause metrics."""

import gc
from pathlib import Path

import pytest

from kvstore.core.exceptions import CommandError
from kvstore.engine import Engine
from kvstore.observability import gcpolicy
from kvstore.observability.metrics import Exposition, process_metrics
from tests.helpers import FakeClock


def test_holds_nest_and_release_unfreezes() -> None:
    assert not gcpolicy.held()
    before = gc.get_freeze_count()
    gcpolicy.hold()
    gcpolicy.hold()
    assert gcpolicy.held()
    assert gc.get_freeze_count() > before  # existing objects are out of the GC's reach
    gcpolicy.release()
    assert gcpolicy.held()  # still one hold
    gcpolicy.release()
    assert not gcpolicy.held()
    assert gc.get_freeze_count() == 0
    gcpolicy.release()  # extra releases are harmless
    assert not gcpolicy.held()


def test_a_rewrite_holds_the_gc_until_it_finishes(data_dir: Path, clock: FakeClock) -> None:
    with Engine(clock=clock, data_dir=data_dir, aof_fsync="no") as engine:
        engine.incremental_snapshots = True
        engine.execute("SET", "a", "1")
        engine.start_rewrite()
        assert gcpolicy.held()
        while engine.step_snapshot():
            pass
        persistence = engine.persistence
        assert persistence is not None
        persistence.wait_rewrite()
        engine.cron()  # finalizes the rewrite and releases
        assert not gcpolicy.held()
        engine.save()  # SAVE releases by itself
        assert not gcpolicy.held()


def test_a_refused_second_rewrite_takes_no_hold(data_dir: Path, clock: FakeClock) -> None:
    with Engine(clock=clock, data_dir=data_dir, aof_fsync="no") as engine:
        engine.execute("SET", "a", "1")
        engine.start_rewrite()
        with pytest.raises(CommandError, match="already in progress"):
            engine.start_rewrite()
        persistence = engine.persistence
        assert persistence is not None
        persistence.wait_rewrite()
        engine.cron()  # finalizes the one rewrite: its one hold is released
        assert not gcpolicy.held()


def test_closing_mid_rewrite_releases(data_dir: Path, clock: FakeClock) -> None:
    engine = Engine(clock=clock, data_dir=data_dir, aof_fsync="no")
    engine.open()
    engine.start_rewrite()  # one-shot copy; the writer thread is running
    engine.close()  # waits for the writer
    assert not gcpolicy.held()


def test_closing_mid_copy_releases_and_loses_nothing(data_dir: Path, clock: FakeClock) -> None:
    """Shutdown during an incremental rewrite: the copy is abandoned, as a crash would."""
    engine = Engine(clock=clock, data_dir=data_dir, aof_fsync="no")
    engine.open()
    engine.incremental_snapshots = True
    engine.snapshot_slice_keys = 10
    engine.snapshot_slice_ms = 0  # one chunk per step
    for i in range(100):
        engine.execute("SET", f"k{i}", str(i))
    engine.start_rewrite()
    assert engine.step_snapshot()  # partly copied
    engine.execute("SET", "after", "start")  # into the new AOF
    engine.close()
    assert not gcpolicy.held()

    with Engine(clock=clock, data_dir=data_dir, aof_fsync="no") as reopened:
        assert reopened.execute("DBSIZE") == 101
        assert reopened.execute("GET", "k99") == "99"
        assert reopened.execute("GET", "after") == "start"


def test_closing_mid_resync_copy_releases(clock: FakeClock) -> None:
    """A replica's full resync copy (ShardNode) abandoned by shutdown: nobody gets it."""
    engine = Engine(clock=clock)
    engine.snapshot_slice_keys = 10
    engine.snapshot_slice_ms = 0
    for i in range(100):
        engine.execute("SET", f"k{i}", str(i))
    delivered: list[object] = []
    engine.begin_snapshot(delivered.append)
    assert gcpolicy.held()
    assert engine.step_snapshot()
    engine.close()
    assert not gcpolicy.held()
    assert not engine.snapshot_in_progress
    assert delivered == []


def test_gc_pauses_are_measured() -> None:
    gcpolicy.install_pause_metrics()
    gcpolicy.install_pause_metrics()  # idempotent
    gc.collect()
    out = Exposition()
    process_metrics(out)
    text = out.text()
    assert 'kvstore_gc_pause_seconds_count{generation="2"}' in text
    assert "kvstore_gc_frozen 0" in text
