"""Build the worker runtime from settings."""

from __future__ import annotations

import httpx

from app.adapters.models.anthropic import OFFICIAL_BASE_URL, AnthropicGateway
from app.adapters.models.replay import ReplayGateway
from app.core.db import create_engine, session_factory
from app.core.security import Sealer
from app.core.settings import Environment, Settings
from app.engine import catalog, indexers
from app.engine.gateway import ModelGateway
from app.jobs.stages import Runtime
from app.main import HTTP_TIMEOUT, github_app, object_store, public_github


def build_runtime(settings: Settings, application_name: str) -> tuple[Runtime, httpx.AsyncClient]:
    engine = create_engine(settings, application_name=application_name)
    http = httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=False)
    gateways: dict[str, ModelGateway] = {"replay": ReplayGateway()}
    if settings.anthropic_api_key is not None:
        gateways["anthropic"] = AnthropicGateway(
            settings.anthropic_api_key.get_secret_value(),
            base_url=settings.anthropic_base_url or OFFICIAL_BASE_URL,
        )
    runtime = Runtime(
        sessions=session_factory(engine),
        store=object_store(settings),
        github=github_app(settings, http),
        public_github=public_github(settings, http),
        allow_unsandboxed=settings.app_env is not Environment.PRODUCTION,
        catalog=catalog.load(settings.model_catalog_file),
        gateways=gateways,
        sealer=Sealer(settings.app_encryption_key.get_secret_value()),
        # M6, T24: a workspace's own key, opened per build; always the official API, never the operator's
        # base URL (08: own keys go only to the provider's official endpoint).
        own_key_gateway=AnthropicGateway,
        indexers=(
            indexers.Config(
                node=settings.scip_node_path,
                tools=settings.scip_tools_dir,
                timeout=settings.scip_timeout_seconds,
                heap_mb=settings.scip_heap_mb,
                rust_analyzer=settings.scip_rust_analyzer_path,
                rust_src=settings.scip_rust_src_dir,
            )
            if (settings.scip_node_path and settings.scip_tools_dir)
            or (settings.scip_rust_analyzer_path and settings.scip_rust_src_dir)
            else None
        ),
    )
    return runtime, http
