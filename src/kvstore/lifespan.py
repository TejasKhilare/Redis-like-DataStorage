"""Startup and shutdown for each node role.

One process serves two planes that share the same event loop:
* data plane    -- the TCP server (low overhead, what clients and the router use);
* control plane -- the FastAPI app (REST API, health, admin, docs).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from kvstore.cluster.router import ShardRouter
from kvstore.core.config import Settings
from kvstore.engine import Engine
from kvstore.engine.expiry import run_active_expiry
from kvstore.protocol.tcp_server import CommandHandler, TCPServer
from kvstore.schemas.common import ReadinessResponse
from kvstore.services.kv_service import LocalKVService, RoutedKVService

logger = logging.getLogger(__name__)


def _tcp_server(settings: Settings, handler: CommandHandler) -> TCPServer | None:
    if not settings.tcp_enabled:
        return None
    return TCPServer(
        handler,
        host=settings.host,
        port=settings.tcp_port,
        max_request_bytes=settings.max_request_bytes,
    )


@asynccontextmanager
async def shard_lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    engine = Engine(
        max_keys=settings.max_keys,
        eviction_policy=settings.eviction_policy,
        aof_path=settings.aof_path if settings.aof_enabled else None,
    )
    engine.open()  # replays the AOF; blocking is fine before we accept traffic
    service = LocalKVService(engine)
    tcp_server = _tcp_server(settings, service.execute)
    expiry_task = asyncio.create_task(
        run_active_expiry(
            engine,
            interval_s=settings.active_expiry_interval_s,
            sample_size=settings.active_expiry_sample_size,
        ),
        name="active-expiry",
    )

    async def readiness_probe() -> ReadinessResponse:
        checks = {"engine": "ok"}
        if tcp_server is not None:
            checks["tcp"] = "ok" if tcp_server.is_serving else "down"
        ready = all(value == "ok" for value in checks.values())
        return ReadinessResponse(status="ready" if ready else "unavailable", checks=checks)

    try:
        if tcp_server is not None:
            await tcp_server.start()
        app.state.engine = engine
        app.state.kv_service = service
        app.state.tcp_server = tcp_server
        app.state.readiness_probe = readiness_probe
        app.state.started_at = time.monotonic()
        info = engine.info()
        logger.info(
            "shard started",
            extra={"keys": info.keys, "aof_records_loaded": info.aof_records_loaded},
        )
        yield
    finally:
        if tcp_server is not None:
            await tcp_server.stop()
        expiry_task.cancel()
        with suppress(asyncio.CancelledError):
            await expiry_task
        engine.close()
        logger.info("shard stopped")


@asynccontextmanager
async def router_lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    shard_router = ShardRouter(
        settings.shards,
        virtual_nodes=settings.virtual_nodes,
        timeout_s=settings.shard_timeout_s,
    )
    tcp_server = _tcp_server(settings, shard_router.execute)

    async def readiness_probe() -> ReadinessResponse:
        statuses = await shard_router.node_status()
        checks = {s.address: "ok" if s.healthy else "down" for s in statuses}
        healthy = sum(s.healthy for s in statuses)
        if healthy == len(statuses):
            return ReadinessResponse(status="ready", checks=checks)
        # Some shards down: keys on healthy shards still work, so stay in rotation.
        return ReadinessResponse(status="degraded" if healthy else "unavailable", checks=checks)

    try:
        if tcp_server is not None:
            await tcp_server.start()
        app.state.router = shard_router
        app.state.kv_service = RoutedKVService(shard_router)
        app.state.tcp_server = tcp_server
        app.state.readiness_probe = readiness_probe
        app.state.started_at = time.monotonic()
        logger.info("router started", extra={"shards": settings.shards})
        yield
    finally:
        if tcp_server is not None:
            await tcp_server.stop()
        await shard_router.close()
        logger.info("router stopped")
