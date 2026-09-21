"""Liveness and readiness probes (unversioned, for load balancers and orchestrators)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from kvstore.schemas.common import HealthResponse, ReadinessResponse

router = APIRouter(tags=["health"])


@router.get("/health")
async def liveness() -> HealthResponse:
    """The process is up and serving HTTP."""
    return HealthResponse()


@router.get("/ready", responses={503: {"model": ReadinessResponse}})
async def readiness(request: Request, response: Response) -> ReadinessResponse:
    """The node can serve traffic. A router with some shards down reports ``degraded``."""
    result: ReadinessResponse = await request.app.state.readiness_probe()
    if result.status == "unavailable":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result
