from __future__ import annotations

from fastapi import APIRouter

from kvstore.api.v1.endpoints import admin, cluster, commands, keys
from kvstore.core.config import NodeRole


def build_v1_router(role: NodeRole) -> APIRouter:
    router = APIRouter(prefix="/v1")
    router.include_router(keys.router)
    router.include_router(commands.router)
    router.include_router(admin.router)
    if role == "router":
        router.include_router(cluster.router)
    return router
