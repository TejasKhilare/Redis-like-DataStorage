"""Node introspection and maintenance."""

from __future__ import annotations

import platform
import time

from fastapi import APIRouter, Request, status

from kvstore import __version__
from kvstore.api.deps import KVServiceDep, SettingsDep
from kvstore.schemas.admin import EngineInfoSchema, NodeInfoResponse, RewriteResponse

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/info")
async def node_info(request: Request, settings: SettingsDep) -> NodeInfoResponse:
    state = request.app.state
    tcp_server = state.tcp_server
    info = NodeInfoResponse(
        node_id=settings.node_id,
        role=settings.node_role,
        version=__version__,
        python_version=platform.python_version(),
        uptime_seconds=round(time.monotonic() - state.started_at, 3),
        tcp_port=tcp_server.port if tcp_server is not None else None,
        connected_clients=tcp_server.connected_clients if tcp_server is not None else None,
    )
    if settings.node_role == "shard":
        info.engine = EngineInfoSchema.model_validate(state.engine.info())
    else:
        info.shards = [group.id for group in state.router.config.shards]
    return info


@router.post("/rewrite", status_code=status.HTTP_202_ACCEPTED)
async def rewrite(service: KVServiceDep) -> RewriteResponse:
    """Start a background snapshot + AOF rewrite (on every shard, via the router)."""
    await service.execute("BGREWRITEAOF")
    return RewriteResponse(status="started")
