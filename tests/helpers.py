from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from kvstore.core.config import Settings
from kvstore.engine import Engine
from kvstore.engine.datatypes import HashValue, ListValue, SetValue, SortedSet
from kvstore.main import create_app


class FakeClock:
    """Deterministic stand-in for ``time.time`` so TTL tests never sleep."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "data_dir": tmp_path / "data",
        "tcp_port": 0,  # ephemeral port
        "access_log": False,
        "cron_interval_s": 0.01,
        "aof_fsync": "no",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@asynccontextmanager
async def running_app(settings: Settings) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    """Run the app's lifespan (engine, TCP server, ...) and give an HTTP client for it."""
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield app, client


def dump(engine: Engine) -> dict[str, tuple[str, Any, float | None]]:
    """Every live key as (type, plain value, expires_at) -- for comparing whole keyspaces."""
    result: dict[str, tuple[str, Any, float | None]] = {}
    for key in engine.store.iter_keys():
        entry = engine.store.peek(key)
        assert entry is not None
        value = entry.value
        plain: Any
        if isinstance(value, str):
            plain = value
        elif isinstance(value, ListValue):
            plain = list(value)
        elif isinstance(value, HashValue):
            plain = dict(value.items())
        elif isinstance(value, SetValue):
            plain = set(value)
        else:
            assert isinstance(value, SortedSet)
            plain = list(value.items())
        result[key] = (type(value).__name__, plain, entry.expires_at)
    return result
