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
    """In-memory engine (no AOF)."""
    return Engine(max_keys=100, clock=clock, rng=random.Random(0))


@pytest.fixture
def aof_path(tmp_path: Path) -> Path:
    return tmp_path / "appendonly.aof"


@pytest.fixture
def durable_engine(clock: FakeClock, aof_path: Path) -> Iterator[Engine]:
    with Engine(max_keys=100, clock=clock, aof_path=aof_path) as engine:
        yield engine
