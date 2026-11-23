"""Build the worker runtime from settings."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from app.adapters.models import anthropic, openai
from app.adapters.models.anthropic import AnthropicGateway
from app.adapters.models.gemini import GeminiGateway
from app.adapters.models.openai import OpenAIGateway
from app.adapters.models.openrouter import OpenRouterGateway
from app.adapters.models.replay import ReplayGateway
from app.core.db import create_engine, session_factory
from app.core.security import sealer_for
from app.core.settings import Environment, Settings
from app.engine import catalog, indexers
from app.engine.gateway import ModelGateway
from app.jobs.stages import Runtime
from app.main import HTTP_TIMEOUT, github_app, object_store, public_github


def operator_gateways(settings: Settings, http: httpx.AsyncClient) -> dict[str, ModelGateway]:
    """A gateway per provider the operator holds a key for; only these may use an operator base URL."""
    gateways: dict[str, ModelGateway] = {"replay": ReplayGateway()}
    if settings.anthropic_api_key is not None:
        gateways["anthropic"] = AnthropicGateway(
            settings.anthropic_api_key.get_secret_value(),
            base_url=settings.anthropic_base_url or anthropic.OFFICIAL_BASE_URL,
        )
    if settings.openai_api_key is not None:
        gateways["openai"] = OpenAIGateway(
            settings.openai_api_key.get_secret_value(),
            client=http,
            base_url=settings.openai_base_url or openai.OFFICIAL_BASE_URL,
        )
    if settings.gemini_api_key is not None:
        gateways["gemini"] = GeminiGateway(settings.gemini_api_key.get_secret_value(), client=http)
    if settings.openrouter_api_key is not None:
        gateways["openrouter"] = OpenRouterGateway(
            settings.openrouter_api_key.get_secret_value(), client=http
        )
    return gateways


def own_key_gateways(http: httpx.AsyncClient) -> Callable[[str, str], ModelGateway]:
    """(provider, workspace key) -> gateway, always on the provider's official API, never an operator base
    URL (08: own keys go only to the provider's official endpoint)."""

    def make(provider: str, key: str) -> ModelGateway:
        if provider == "anthropic":
            return AnthropicGateway(key)
        if provider == "openai":
            return OpenAIGateway(key, client=http)
        if provider == "gemini":
            return GeminiGateway(key, client=http)
        if provider == "openrouter":
            return OpenRouterGateway(key, client=http)
        raise ValueError(f"no own-key gateway for {provider}")

    return make


def build_runtime(settings: Settings, application_name: str) -> tuple[Runtime, httpx.AsyncClient]:
    engine = create_engine(settings, application_name=application_name)
    http = httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=False)
    runtime = Runtime(
        sessions=session_factory(engine),
        store=object_store(settings),
        github=github_app(settings, http),
        public_github=public_github(settings, http),
        allow_unsandboxed=settings.app_env is not Environment.PRODUCTION,
        catalog=catalog.load(settings.model_catalog_file),
        gateways=operator_gateways(settings, http),
        sealer=sealer_for(settings),
        own_key_gateway=own_key_gateways(http),  # M6, T24: a workspace's own key, opened per build
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
