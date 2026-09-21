import asyncio
import logging
from contextlib import suppress

import pytest

from kvstore.engine import Engine
from kvstore.engine.cron import run_cron
from tests.helpers import FakeClock


async def run_briefly(engine: Engine, seconds: float = 0.1) -> None:
    task = asyncio.create_task(run_cron(engine, interval_s=0.005, expiry_sample_size=20))
    await asyncio.sleep(seconds)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_cron_expires_keys(clock: FakeClock) -> None:
    engine = Engine(clock=clock)
    for i in range(50):
        engine.execute("SET", f"k{i}", "v", "EX", "1")
    clock.advance(2)
    await run_briefly(engine)
    assert engine.info().keys == 0
    assert engine.info().expired_keys == 50


async def test_cron_survives_errors(
    clock: FakeClock, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = Engine(clock=clock)
    calls = 0

    def flaky(sample_size: int = 20) -> int:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(engine, "cron", flaky)
    with caplog.at_level(logging.ERROR):
        await run_briefly(engine)
    assert calls > 1
    assert "cron tick failed" in caplog.text
