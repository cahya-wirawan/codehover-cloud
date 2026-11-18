"""Build the worker runtime from settings."""

from __future__ import annotations

import httpx

from app.core.db import create_engine, session_factory
from app.core.settings import Environment, Settings
from app.jobs.stages import Runtime
from app.main import HTTP_TIMEOUT, github_app, object_store


def build_runtime(settings: Settings, application_name: str) -> tuple[Runtime, httpx.AsyncClient]:
    engine = create_engine(settings, application_name=application_name)
    http = httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=False)
    runtime = Runtime(
        sessions=session_factory(engine),
        store=object_store(settings),
        github=github_app(settings, http),
        allow_unsandboxed=settings.app_env is not Environment.PRODUCTION,
    )
    return runtime, http
