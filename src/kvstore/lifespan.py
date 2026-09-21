"""Startup and shutdown for each node role.

One process serves two planes that share the same event loop:
* data plane    -- the RESP TCP server (what clients, redis-cli and the router use);
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
from kvstore.engine.cron import run_cron
from kvstore.protocol.tcp_server import BatchContext, CommandHandler, TCPServer
from kvstore.schemas.common import ReadinessResponse
from kvstore.services.kv_service import LocalKVService, RoutedKVService

logger = logging.getLogger(__name__)


def _tcp_server(
    settings: Settings, handler: CommandHandler, batch: BatchContext | None = None
) -> TCPServer | None:
    if not settings.tcp_enabled:
        return None
    return TCPServer(
        handler,
        host=settings.host,
        port=settings.tcp_port,
        max_request_bytes=settings.max_request_bytes,
        batch=batch,
    )


def build_engine(settings: Settings) -> Engine:
    return Engine(
        max_keys=settings.max_keys,
        maxmemory=settings.maxmemory_bytes,
        eviction_policy=settings.eviction_policy,
        data_dir=settings.data_dir if settings.aof_enabled else None,
        aof_fsync=settings.aof_fsync,
        aof_rewrite_percentage=settings.aof_rewrite_percentage,
        aof_rewrite_min_bytes=settings.aof_rewrite_min_bytes,
    )


@asynccontextmanager
async def shard_lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    engine = build_engine(settings)
    engine.open()  # loads snapshot + AOF; blocking is fine before we accept traffic
    service = LocalKVService(engine)
    # The engine's execute is synchronous, so a pipelined batch runs atomically
    # and commits the AOF once (group commit) before any reply goes out.
    tcp_server = _tcp_server(settings, engine.execute, engine.deferred_commit)
    cron_task = asyncio.create_task(
        run_cron(
            engine,
            interval_s=settings.cron_interval_s,
            expiry_sample_size=settings.active_expiry_sample_size,
        ),
        name="cron",
    )

    async def readiness_probe() -> ReadinessResponse:
        checks = {"engine": "ok"}
        persistence = engine.persistence
        if persistence is not None:
            checks["persistence"] = "down" if persistence.write_error else "ok"
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
            extra={
                "keys": info.keys,
                "snapshot_keys_loaded": info.persistence.snapshot_keys_loaded
                if info.persistence
                else 0,
                "aof_records_loaded": info.persistence.aof_records_loaded
                if info.persistence
                else 0,
            },
        )
        yield
    finally:
        if tcp_server is not None:
            await tcp_server.stop()
        cron_task.cancel()
        with suppress(asyncio.CancelledError):
            await cron_task
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
