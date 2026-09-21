"""Background task that runs the engine's active-expiry cycle."""

from __future__ import annotations

import asyncio
import logging

from kvstore.engine.engine import Engine

logger = logging.getLogger(__name__)


async def run_active_expiry(engine: Engine, *, interval_s: float, sample_size: int) -> None:
    """Run forever on the event loop (so it never races command execution); cancel to stop.

    Redis runs the same job 10 times a second (``hz 10``).
    """
    while True:
        await asyncio.sleep(interval_s)
        try:
            expired = engine.run_expiry_cycle(sample_size)
        except Exception:
            logger.exception("active expiry cycle failed")
            continue
        if expired:
            logger.debug("active expiry removed keys", extra={"expired": expired})
