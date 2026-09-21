"""FastAPI application factory.

Run with ``python -m kvstore`` (or ``uvicorn --factory kvstore.main:create_app``).
Always a single worker process: each worker would own a separate keyspace.
"""

from __future__ import annotations

from fastapi import FastAPI

from kvstore import __version__
from kvstore.api import health
from kvstore.api.errors import register_exception_handlers
from kvstore.api.middleware import install_request_middleware
from kvstore.api.v1.router import build_v1_router
from kvstore.core.config import Settings, get_settings
from kvstore.lifespan import router_lifespan, shard_lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    is_router = settings.node_role == "router"

    app = FastAPI(
        title="kvstore",
        summary="Redis-inspired distributed in-memory key-value store",
        version=__version__,
        lifespan=router_lifespan if is_router else shard_lifespan,
    )
    app.state.settings = settings

    register_exception_handlers(app)
    install_request_middleware(app, access_log=settings.access_log)
    app.include_router(health.router)
    app.include_router(build_v1_router(settings.node_role))
    return app
