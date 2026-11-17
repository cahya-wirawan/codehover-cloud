"""FastAPI application factory (16_REPO_STRUCTURE.md): middleware, error envelope and routers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from app import __version__
from app.api import health
from app.core.db import create_engine, session_factory
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.core.request_context import RequestIdMiddleware
from app.core.settings import Environment, Settings, get_settings

API_PREFIX = "/api/v1"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = create_engine(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await engine.dispose()

    interactive_docs = settings.app_env is not Environment.PRODUCTION
    app = FastAPI(
        title="codehover-cloud API",
        version=__version__,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs" if interactive_docs else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessions = session_factory(engine)

    install_error_handlers(app)
    app.add_middleware(RequestIdMiddleware)

    api = APIRouter(prefix=API_PREFIX)
    api.include_router(health.router)
    app.include_router(api)
    return app


def build() -> FastAPI:
    """Entry point for `uvicorn app.main:app`: configure logging once, then build from the environment."""
    settings = get_settings()
    configure_logging(settings.log_level)
    return create_app(settings)


def __getattr__(name: str) -> FastAPI:
    # Lazily build `app` so importing this module (tests, OpenAPI export) never reads the environment.
    if name == "app":
        application = build()
        globals()["app"] = application
        return application
    raise AttributeError(name)
