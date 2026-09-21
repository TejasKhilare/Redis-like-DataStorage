"""FastAPI dependencies. Runtime objects live on ``app.state``, created by the lifespan."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from kvstore.cluster.manager import ClusterManager
from kvstore.cluster.router import ShardRouter
from kvstore.core.config import Settings
from kvstore.engine import Engine
from kvstore.services.kv_service import KVService


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_kv_service(request: Request) -> KVService:
    service: KVService = request.app.state.kv_service
    return service


def get_engine(request: Request) -> Engine:
    engine: Engine = request.app.state.engine
    return engine


def get_router(request: Request) -> ShardRouter:
    router: ShardRouter = request.app.state.router
    return router


def get_manager(request: Request) -> ClusterManager:
    manager: ClusterManager = request.app.state.manager
    return manager


SettingsDep = Annotated[Settings, Depends(get_settings)]
KVServiceDep = Annotated[KVService, Depends(get_kv_service)]
EngineDep = Annotated[Engine, Depends(get_engine)]
RouterDep = Annotated[ShardRouter, Depends(get_router)]
ManagerDep = Annotated[ClusterManager, Depends(get_manager)]
