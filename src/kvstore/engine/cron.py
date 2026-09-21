"""Background task that runs the engine's periodic housekeeping."""

from __future__ import annotations

import asyncio
import logging

from kvstore.engine.engine import Engine

logger = logging.getLogger(__name__)


async def run_cron(engine: Engine, *, interval_s: float, expiry_sample_size: int) -> None:
    """Run forever on the event loop (so it never races command execution); cancel to stop.

    Each tick: active expiry, finishing a completed background rewrite, and
    starting an automatic one when the AOF has grown enough. Redis runs its
    ``serverCron`` 10 times a second (``hz 10``).
    """
    while True:
        await asyncio.sleep(interval_s)
        try:
            expired = engine.cron(expiry_sample_size)
        except Exception:
            logger.exception("cron tick failed")
            continue
        if expired:
            logger.debug("active expiry removed keys", extra={"expired": expired})
