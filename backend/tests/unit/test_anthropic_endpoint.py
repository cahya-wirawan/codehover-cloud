"""Where Anthropic calls go (07, 08): the official API or the operator's ANTHROPIC_BASE_URL, never an SDK fallback.

The SDK would otherwise read the `ANTHROPIC_BASE_URL` environment variable or a credentials profile when no
base URL is passed, so every client is created with one. The variable still configures the operator's
endpoint, but only as the validated setting, and a workspace's own key always goes to the official API.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.adapters.models.anthropic import OFFICIAL_BASE_URL, AnthropicGateway
from app.jobs.runtime import build_runtime
from app.modules.providers import keys
from tests.conftest import make_settings

ELSEWHERE = "https://elsewhere.invalid"


def _host(gateway: Any) -> str:
    return str(gateway.client.base_url).rstrip("/")


def test_the_setting_is_an_https_url_and_blank_means_the_official_api() -> None:
    assert make_settings().anthropic_base_url is None
    assert make_settings(anthropic_base_url="  ").anthropic_base_url is None
    proxy = make_settings(anthropic_base_url="https://llm-gateway.example.test/anthropic/")
    assert proxy.anthropic_base_url == "https://llm-gateway.example.test/anthropic"
    for bad in (
        "http://llm-gateway.example.test",
        "https://user:pw@llm-gateway.example.test",
        "https://llm-gateway.example.test/?k=v",
        "https://llm-gateway.example.test/#x",
        "llm-gateway.example.test",
        "https://",
    ):
        with pytest.raises(ValidationError, match="ANTHROPIC_BASE_URL"):
            make_settings(anthropic_base_url=bad)


def test_the_sdk_environment_fallback_is_never_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", ELSEWHERE)
    assert _host(AnthropicGateway("k")) == OFFICIAL_BASE_URL
    assert _host(AnthropicGateway("k", base_url="https://proxy.example.test")) == "https://proxy.example.test"


def test_the_operator_endpoint_applies_only_to_the_operator_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ANTHROPIC_BASE_URL` in the environment is the validated setting itself; it reaches only the
    operator's gateway."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", ELSEWHERE)
    assert make_settings().anthropic_base_url == ELSEWHERE
    settings = make_settings(
        anthropic_api_key=SecretStr("sk-operator"), anthropic_base_url="https://proxy.example.test/v"
    )
    runtime, http = build_runtime(settings, "test")
    try:
        assert _host(runtime.gateways["anthropic"]) == "https://proxy.example.test/v"
        assert (
            _host(runtime.own_key_gateway("anthropic", "sk-workspace")) == OFFICIAL_BASE_URL
        )  # own keys: official only
    finally:
        asyncio.run(http.aclose())

    monkeypatch.delenv("ANTHROPIC_BASE_URL")
    runtime, http = build_runtime(make_settings(anthropic_api_key=SecretStr("sk-operator")), "test")
    try:
        assert _host(runtime.gateways["anthropic"]) == OFFICIAL_BASE_URL  # unset: the official API
    finally:
        asyncio.run(http.aclose())


def test_saving_an_own_key_verifies_it_with_the_official_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", ELSEWHERE)
    created: list[dict[str, Any]] = []

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)
            self.models = self

        async def list(self, **_: Any) -> None:
            return None

        async def close(self) -> None:
            return None

    monkeypatch.setattr(keys.anthropic, "AsyncAnthropic", Client)
    assert asyncio.run(keys.verify_with_provider("anthropic", "sk-workspace")) is True
    assert created[0]["base_url"] == OFFICIAL_BASE_URL
