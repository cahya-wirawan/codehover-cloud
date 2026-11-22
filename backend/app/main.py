"""FastAPI application factory (16_REPO_STRUCTURE.md): middleware, error envelope and routers."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import APIRouter, FastAPI, Request, Response
from starlette.middleware.base import RequestResponseEndpoint

from app import __version__
from app.adapters.billing import billing_provider
from app.adapters.github.app import GitHubApp
from app.adapters.github.public import PublicGitHub
from app.adapters.oauth import OAuthProvider
from app.adapters.oauth.github import GitHubProvider
from app.adapters.oauth.google import GoogleProvider
from app.adapters.storage import MemoryObjectStore, ObjectStore, S3ObjectStore
from app.api import health
from app.core import metrics
from app.core.db import create_engine, session_factory
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.core.request_context import RequestIdMiddleware
from app.core.security import Sealer
from app.core.settings import Environment, Settings, get_settings
from app.engine import catalog
from app.modules.billing import router as billing
from app.modules.deletion import router as deletion
from app.modules.github import router as github
from app.modules.identity import router as identity
from app.modules.mcp import oauth as mcp_oauth
from app.modules.mcp import router as mcp
from app.modules.ops import router as ops
from app.modules.projects import router as projects
from app.modules.providers import keys as provider_keys
from app.modules.providers import router as providers
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


def public_github(settings: Settings, http: httpx.AsyncClient) -> PublicGitHub:
    token = settings.github_public_token
    return PublicGitHub(http, token.get_secret_value() if token is not None else None)


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


async def observe(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """Request counts and durations by route template (12); event streams are counted, not timed."""
    started = time.perf_counter()
    response = await call_next(request)
    route = metrics.route_of(request.scope)
    metrics.HTTP_REQUESTS.labels(route, request.method, f"{response.status_code // 100}xx").inc()
    if response.media_type != "text/event-stream":
        metrics.HTTP_DURATION.labels(route, request.method).observe(time.perf_counter() - started)
    return response


async def no_store(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """API responses are private and never cached by browsers or intermediaries (08)."""
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
        # Defense in depth beside the gateway's headers: tenant text is never sniffed as HTML (A07).
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = create_engine(settings)
    http = httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=False)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        metrics.serve(settings.metrics_port)
        try:
            yield
        finally:
            await http.aclose()
            await engine.dispose()
            await limiter.dispose()

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
    # Rate-limit hits run on their own small pool: a request already holds a connection of the main
    # pool, and taking a second one from it could exhaust the pool against itself under load.
    limiter = create_engine(settings, application_name="ratelimit", pool_size=4, max_overflow=4)
    app.state.rate_sessions = session_factory(limiter)
    app.state.http = http
    app.state.sealer = Sealer(settings.app_encryption_key.get_secret_value())
    app.state.oauth_providers = oauth_providers(settings, http)
    app.state.github_app = github_app(settings, http)
    app.state.public_github = public_github(settings, http)
    app.state.objects = object_store(settings)
    app.state.catalog = catalog.load(settings.model_catalog_file)
    app.state.billing = billing_provider(settings, http)
    app.state.key_verifier = provider_keys.verify_with_provider

    install_error_handlers(app)
    app.middleware("http")(no_store)
    app.middleware("http")(observe)
    app.add_middleware(RequestIdMiddleware)

    api = APIRouter(prefix=API_PREFIX)
    api.include_router(health.router)
    api.include_router(identity.router)
    api.include_router(workspaces.router)
    api.include_router(github.router)
    api.include_router(projects.router)
    api.include_router(published.router)
    api.include_router(mcp.router)
    api.include_router(mcp_oauth.router)
    api.include_router(usage.router)
    api.include_router(billing.router)
    api.include_router(providers.router)
    api.include_router(webhooks.router)
    api.include_router(deletion.router)
    api.include_router(ops.router)
    app.include_router(api)
    app.include_router(mcp_oauth.well_known)  # /.well-known/oauth-* at the origin's root (RFC 8414, 9728)
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
