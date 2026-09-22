from __future__ import annotations

import random
from collections.abc import Iterator
from pathlib import Path

import pytest

from kvstore.engine import Engine
from kvstore.observability import gcpolicy
from tests.helpers import FakeClock


@pytest.fixture(autouse=True)
def _no_frozen_gc_left_behind() -> Iterator[None]:
    """Fail the test that leaves CPython's collector frozen, not whichever runs next.

    A snapshot holds ``gc.freeze()`` until it finishes; one never finished or
    released would keep cyclic garbage uncollected for the rest of the process.
    """
    yield
    assert not gcpolicy.held(), "the collector is still frozen: a snapshot was not released"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def engine(clock: FakeClock) -> Engine:
    """In-memory engine (no persistence)."""
    return Engine(clock=clock, rng=random.Random(0))


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def durable_engine(clock: FakeClock, data_dir: Path) -> Iterator[Engine]:
    with Engine(clock=clock, data_dir=data_dir, aof_fsync="no", rng=random.Random(0)) as engine:
        yield engine
