from __future__ import annotations

import random
from collections.abc import Iterator
from pathlib import Path

import pytest

from kvstore.engine import Engine
from tests.helpers import FakeClock


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
