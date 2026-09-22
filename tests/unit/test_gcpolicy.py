"""The GC hold that keeps full collections out of snapshot copies, and GC pause metrics."""

import gc
from pathlib import Path

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


def test_closing_mid_rewrite_releases(data_dir: Path, clock: FakeClock) -> None:
    engine = Engine(clock=clock, data_dir=data_dir, aof_fsync="no")
    engine.open()
    engine.start_rewrite()  # one-shot copy; the writer thread is running
    engine.close()  # waits for the writer
    assert not gcpolicy.held()


def test_gc_pauses_are_measured() -> None:
    gcpolicy.install_pause_metrics()
    gcpolicy.install_pause_metrics()  # idempotent
    gc.collect()
    out = Exposition()
    process_metrics(out)
    text = out.text()
    assert 'kvstore_gc_pause_seconds_count{generation="2"}' in text
    assert "kvstore_gc_frozen 0" in text
