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

from kvstore.cluster.manager import ClusterManager
from kvstore.cluster.router import ShardRouter
from kvstore.cluster.topology import ClusterConfig
from kvstore.core.config import Settings
from kvstore.engine import Engine
from kvstore.engine.cron import run_cron
from kvstore.observability import gcpolicy
from kvstore.protocol.tcp_server import BatchHandler, CommandHandler, GroupCommit, TCPServer
from kvstore.replication.node import ReplicationSettings, ShardNode
from kvstore.schemas.common import ReadinessResponse
from kvstore.services.kv_service import LocalKVService, RoutedKVService

logger = logging.getLogger(__name__)


def _tcp_server(
    settings: Settings,
    handler: CommandHandler,
    *,
    group_commit: GroupCommit | None = None,
    batch_handler: BatchHandler | None = None,
) -> TCPServer | None:
    if not settings.tcp_enabled:
        return None
    return TCPServer(
        handler,
        host=settings.host,
        port=settings.tcp_port,
        max_request_bytes=settings.max_request_bytes,
        group_commit=group_commit,
        batch_handler=batch_handler,
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
    gcpolicy.install_pause_metrics()
    engine.incremental_snapshots = settings.incremental_snapshots  # run_cron drives the copy
    engine.snapshot_slice_keys = settings.snapshot_slice_keys
    engine.snapshot_slice_ms = settings.snapshot_slice_ms
    # The engine's execute is synchronous, so a pipelined batch runs atomically.
    # Every connection served in one loop iteration then shares one AOF commit
    # (and fsync) before any of them gets a reply.
    group_commit = GroupCommit(engine.hold_commit, engine.release_commit)
    node = ShardNode(
        engine,
        listening_port=settings.tcp_port,
        data_dir=settings.data_dir if settings.aof_enabled else None,
        settings=ReplicationSettings(
            backlog_bytes=settings.repl_backlog_bytes,
            timeout_s=settings.repl_timeout_s,
            ping_interval_s=settings.repl_ping_interval_s,
            min_replicas_to_write=settings.min_replicas_to_write,
            min_replicas_max_lag_s=settings.min_replicas_max_lag_s,
        ),
        metrics=settings.metrics_enabled,
    )
    service = LocalKVService(node, group_commit)
    tcp_server = _tcp_server(settings, node.execute, group_commit=group_commit)
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
            node.listening_port = tcp_server.port  # the real one when started on port 0
        node.start(replicaof=settings.replicaof)
        app.state.engine = engine
        app.state.node = node
        app.state.group_commit = group_commit
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
        await node.stop()
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
    # A saved config (it records every failover and rebalance) wins over KV_SHARDS.
    assert settings.data_dir is not None
    config_path = settings.data_dir / "cluster.json"
    config = ClusterConfig.load(config_path) or ClusterConfig.from_spec(
        settings.shards, virtual_nodes=settings.virtual_nodes
    )
    shard_router = ShardRouter(
        config,
        timeout_s=settings.shard_timeout_s,
        pool_size=settings.shard_pool_size,
        retries=settings.shard_retries,
        retry_backoff_s=settings.shard_retry_backoff_s,
        wait_replicas=settings.wait_replicas,
        wait_timeout_s=settings.wait_timeout_s,
        read_from_replicas=settings.read_from_replicas,
    )
    tcp_server = _tcp_server(
        settings, shard_router.execute, batch_handler=shard_router.execute_batch
    )
    manager = ClusterManager(
        config,
        on_change=shard_router.apply_config,
        config_path=config_path,
        heartbeat_interval_s=settings.heartbeat_interval_s,
        suspect_after_s=settings.suspect_after_s,
        dead_after_s=settings.dead_after_s,
        failover_enabled=settings.failover_enabled,
    )
    shard_router.is_healthy = manager.is_healthy

    async def readiness_probe() -> ReadinessResponse:
        statuses = await shard_router.node_status()
        checks = {s.address: "ok" if s.healthy else "down" for s in statuses}
        primaries = [s for s in statuses if s.role == "primary"]
        up = sum(s.healthy for s in primaries)
        if up == len(primaries):  # a replica being down costs no availability
            return ReadinessResponse(status="ready", checks=checks)
        # Some groups down: keys on healthy groups still work, so stay in rotation.
        return ReadinessResponse(status="degraded" if up else "unavailable", checks=checks)

    try:
        if tcp_server is not None:
            await tcp_server.start()
        manager.start()
        app.state.router = shard_router
        app.state.manager = manager
        app.state.kv_service = RoutedKVService(shard_router)
        app.state.tcp_server = tcp_server
        app.state.readiness_probe = readiness_probe
        app.state.started_at = time.monotonic()
        logger.info("router started", extra={"config": config.to_dict()})
        yield
    finally:
        await manager.stop()
        if tcp_server is not None:
            await tcp_server.stop()
        await shard_router.close()
        logger.info("router stopped")
