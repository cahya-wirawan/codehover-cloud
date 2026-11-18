"""FastAPI application factory (16_REPO_STRUCTURE.md): middleware, error envelope and routers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import APIRouter, FastAPI, Request, Response
from starlette.middleware.base import RequestResponseEndpoint

from app import __version__
from app.adapters.github.app import GitHubApp
from app.adapters.oauth import OAuthProvider
from app.adapters.oauth.github import GitHubProvider
from app.adapters.oauth.google import GoogleProvider
from app.adapters.storage import MemoryObjectStore, ObjectStore, S3ObjectStore
from app.api import health
from app.core.db import create_engine, session_factory
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.core.request_context import RequestIdMiddleware
from app.core.security import Sealer
from app.core.settings import Environment, Settings, get_settings
from app.engine import catalog
from app.modules.deletion import router as deletion
from app.modules.github import router as github
from app.modules.identity import router as identity
from app.modules.projects import router as projects
from app.modules.published import router as published
from app.modules.usage import router as usage
from app.modules.webhooks import router as webhooks
from app.modules.workspaces import router as workspaces

API_PREFIX = "/api/v1"
# Explicit timeouts on every outbound call (04); one shared client per process.
HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def oauth_providers(settings: Settings, http: httpx.AsyncClient) -> dict[str, OAuthProvider]:
    """Only providers with complete credentials are enabled; others report "unavailable"."""
    providers: dict[str, OAuthProvider] = {}
    if settings.google_client_id and settings.google_client_secret:
        providers["google"] = GoogleProvider(
            settings.google_client_id, settings.google_client_secret.get_secret_value(), http
        )
    if settings.github_oauth_client_id and settings.github_oauth_client_secret:
        providers["github"] = GitHubProvider(
            settings.github_oauth_client_id, settings.github_oauth_client_secret.get_secret_value(), http
        )
    return providers


def object_store(settings: Settings) -> ObjectStore:
    if not settings.s3_endpoint_url:
        if settings.app_env is Environment.PRODUCTION:
            raise RuntimeError("S3 storage must be configured in production")
        return MemoryObjectStore()
    from app.ops.bootstrap import s3_client

    return S3ObjectStore(s3_client(settings), settings.s3_bucket)


def github_app(settings: Settings, http: httpx.AsyncClient) -> GitHubApp | None:
    """Repository access needs the App's ID, slug, client credentials and a real private key."""
    required = (
        settings.github_app_id,
        settings.github_app_slug,
        settings.github_app_client_id,
        settings.github_app_client_secret,
        settings.github_app_private_key_file,
    )
    if not all(required):
        return None
    try:
        pem = Path(str(settings.github_app_private_key_file)).read_text()
    except OSError:
        return None
    if "PRIVATE KEY" not in pem:  # e.g. the empty local placeholder
        return None
    assert settings.github_app_client_secret is not None
    return GitHubApp(
        app_id=str(settings.github_app_id),
        slug=str(settings.github_app_slug),
        client_id=str(settings.github_app_client_id),
        client_secret=settings.github_app_client_secret.get_secret_value(),
        private_key_pem=pem,
        http=http,
    )


async def no_store(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """API responses are private and never cached by browsers or intermediaries (08)."""
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = create_engine(settings)
    http = httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=False)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await http.aclose()
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
    app.state.http = http
    app.state.sealer = Sealer(settings.app_encryption_key.get_secret_value())
    app.state.oauth_providers = oauth_providers(settings, http)
    app.state.github_app = github_app(settings, http)
    app.state.objects = object_store(settings)
    app.state.catalog = catalog.load(settings.model_catalog_file)

    install_error_handlers(app)
    app.middleware("http")(no_store)
    app.add_middleware(RequestIdMiddleware)

    api = APIRouter(prefix=API_PREFIX)
    api.include_router(health.router)
    api.include_router(identity.router)
    api.include_router(workspaces.router)
    api.include_router(github.router)
    api.include_router(projects.router)
    api.include_router(published.router)
    api.include_router(usage.router)
    api.include_router(webhooks.router)
    api.include_router(deletion.router)
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
