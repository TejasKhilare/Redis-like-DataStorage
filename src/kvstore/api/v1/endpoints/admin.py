"""Node introspection."""

from __future__ import annotations

import platform
import time

from fastapi import APIRouter, Request

from kvstore import __version__
from kvstore.api.deps import SettingsDep
from kvstore.schemas.admin import EngineInfoSchema, NodeInfoResponse

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
    )
    if settings.node_role == "shard":
        info.engine = EngineInfoSchema.model_validate(state.engine.info())
    else:
        info.shards = state.router.ring.nodes
    return info
