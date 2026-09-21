import asyncio
import logging
from contextlib import suppress

import pytest

from kvstore.engine import Engine
from kvstore.engine.expiry import run_active_expiry
from tests.helpers import FakeClock


async def run_briefly(engine: Engine, seconds: float = 0.1) -> None:
    task = asyncio.create_task(run_active_expiry(engine, interval_s=0.005, sample_size=20))
    await asyncio.sleep(seconds)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_background_task_expires_keys(clock: FakeClock) -> None:
    engine = Engine(clock=clock)
    for i in range(50):
        engine.execute("SET", f"k{i}", i, "EX", 1)
    clock.advance(2)

    await run_briefly(engine)

    assert engine.info().keys == 0
    assert engine.info().expired_keys == 50


async def test_background_task_survives_errors(
    clock: FakeClock, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = Engine(clock=clock)
    calls = 0

    def flaky(sample_size: int) -> int:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(engine, "run_expiry_cycle", flaky)
    with caplog.at_level(logging.ERROR):
        await run_briefly(engine)

    assert calls > 1  # kept running after the first failure
    assert "active expiry cycle failed" in caplog.text
