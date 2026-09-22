"""Prometheus scrape endpoint (both roles)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from kvstore.observability.collect import router_metrics, shard_metrics
from kvstore.observability.metrics import CONTENT_TYPE

router = APIRouter(tags=["observability"])


@router.get("/metrics", response_class=Response)
async def metrics(request: Request) -> Response:
    """Metrics in the Prometheus text format (scrape this)."""
    state = request.app.state
    settings = state.settings
    if settings.node_role == "shard":
        body = shard_metrics(
            state.node,
            node_id=settings.node_id,
            group_commit=state.group_commit,
            tcp=state.tcp_server,
        )
    else:
        body = router_metrics(
            state.router, node_id=settings.node_id, manager=state.manager, tcp=state.tcp_server
        )
    return Response(content=body, media_type=CONTENT_TYPE)
