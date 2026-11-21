"""OpenAI, Gemini and OpenRouter adapters (07 "Provider abstraction"), against `httpx.MockTransport`.

Each adapter must send the strict schema, cap output, call only its official endpoint (or the operator's
OpenAI base URL), normalize usage so cached input is priced apart, and map every failure to the gateway's
error kinds without leaking response text.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.adapters.models import gemini, openai, openrouter
from app.adapters.models.anthropic import AnthropicGateway
from app.engine import catalog
from app.engine.catalog import Model, Price, Role
from app.engine.gateway import GenerationRequest, ProviderError, Usage
from app.jobs.runtime import operator_gateways, own_key_gateways
from app.modules.providers import keys
from tests.conftest import make_settings

SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string", "maxLength": 100}},
    "required": ["summary"],
    "additionalProperties": False,
}
ANSWER = {"summary": "Adds two numbers."}
ANSWER_TEXT = json.dumps(ANSWER)
Handler = Callable[[httpx.Request], httpx.Response]


def request(provider: str, effort: str | None = None) -> GenerationRequest:
    model = Model(
        id=f"{provider}/test-model",
        provider=provider,  # type: ignore[arg-type]
        provider_model="test-model",
        context_tokens=100_000,
        max_output_tokens=8192,
        structured_output=True,
        data_region="us",
        retention="test",
        price=Price(input_per_mtok_micro_usd=2_500_000, output_per_mtok_micro_usd=10_000_000),
    )
    return GenerationRequest(
        model=model,
        role=Role(model=model.id, max_output_tokens=4096, effort=effort),  # type: ignore[arg-type]
        purpose="explain",
        prompt_version="0.4.0",
        system="You explain code.",
        user="Explain add().",
        schema=SCHEMA,
    )


def run(gateway_of: Callable[[httpx.AsyncClient], Any], handler: Handler, provider: str, **kw: Any) -> Any:
    async def go() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await gateway_of(http).generate(request(provider, **kw))

    return asyncio.run(go())


def failure(gateway_of: Callable[[httpx.AsyncClient], Any], handler: Handler, provider: str) -> ProviderError:
    with pytest.raises(ProviderError) as caught:
        run(gateway_of, handler, provider)
    return caught.value


# ---------------------------------------------------------------- OpenAI
def openai_gw(http: httpx.AsyncClient) -> openai.OpenAIGateway:
    return openai.OpenAIGateway("sk-test", client=http)


def openai_ok(text: str = ANSWER_TEXT, **extra: Any) -> dict[str, Any]:
    return {
        "id": "resp_1",
        "status": "completed",
        "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]},
        ],
        "usage": {
            "input_tokens": 1000,
            "input_tokens_details": {"cached_tokens": 400},
            "output_tokens": 300,
            "output_tokens_details": {"reasoning_tokens": 200},
        },
        **extra,
    }


def test_openai_sends_a_strict_schema_and_prices_cached_input_apart() -> None:
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return httpx.Response(200, json=openai_ok(), headers={"x-request-id": "req_9"})

    result = run(openai_gw, handler, "openai", effort="high")
    assert result.output == ANSWER and result.provider_request_id == "req_9"
    assert result.usage == Usage(input_tokens=600, output_tokens=300, cache_read_tokens=400)
    sent = seen[0]
    assert str(sent.url) == "https://api.openai.com/v1/responses"
    assert sent.headers["authorization"] == "Bearer sk-test"
    body = json.loads(sent.content)
    assert body["text"]["format"] == {
        "type": "json_schema",
        "name": "explain",
        "schema": {k: v for k, v in SCHEMA.items()} | {"properties": {"summary": {"type": "string"}}},
        "strict": True,
    }
    assert body["instructions"] == "You explain code." and body["store"] is False
    assert body["max_output_tokens"] == 4096 and body["reasoning"] == {"effort": "high"}


def test_openai_without_effort_sends_no_reasoning() -> None:
    seen: list[dict[str, Any]] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(json.loads(r.content))
        return httpx.Response(200, json=openai_ok())

    run(openai_gw, handler, "openai")
    assert "reasoning" not in seen[0]


@pytest.mark.parametrize(
    ("body", "kind", "code"),
    [
        (
            openai_ok(status="incomplete", incomplete_details={"reason": "max_output_tokens"}),
            "invalid",
            "output_truncated",
        ),
        (
            openai_ok(status="incomplete", incomplete_details={"reason": "content_filter"}),
            "permanent",
            "provider_refusal",
        ),
        (openai_ok(status="failed"), "transient", "provider_unavailable"),
        (openai_ok(text="not json"), "invalid", "output_not_json"),
        (
            {
                **openai_ok(),
                "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}],
            },
            "permanent",
            "provider_refusal",
        ),
    ],
)
def test_openai_answers_that_are_not_usable(body: dict[str, Any], kind: str, code: str) -> None:
    error = failure(openai_gw, lambda r: httpx.Response(200, json=body), "openai")
    assert (error.kind, error.code) == (kind, code)
    assert error.usage is not None and error.usage.output_tokens == 300  # billed usage is kept


@pytest.mark.parametrize(
    ("status", "body", "kind", "code"),
    [
        (401, {"error": {"code": "invalid_api_key"}}, "permanent", "provider_auth"),
        (429, {"error": {"code": "rate_limit_exceeded"}}, "transient", "provider_rate_limited"),
        (429, {"error": {"code": "insufficient_quota"}}, "permanent", "provider_quota"),
        (400, {"error": {"message": "secret echoed input"}}, "permanent", "provider_rejected"),
        (503, {}, "transient", "provider_unavailable"),
        (408, {}, "unknown", "provider_timeout"),
    ],
)
def test_openai_errors(status: int, body: dict[str, Any], kind: str, code: str) -> None:
    error = failure(
        openai_gw, lambda r: httpx.Response(status, json=body, headers={"retry-after": "7"}), "openai"
    )
    assert (error.kind, error.code) == (kind, code)
    assert "secret" not in str(error)
    if code == "provider_rate_limited":
        assert error.retry_after_seconds == 7.0


def test_transport_failures_are_transient_only_when_nothing_was_sent() -> None:
    def refuse(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=r)

    def hang(r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=r)

    assert failure(openai_gw, refuse, "openai").kind == "transient"
    assert failure(openai_gw, hang, "openai").kind == "unknown"
    assert failure(openai_gw, lambda r: httpx.Response(200, text="<html>"), "openai").kind == "unknown"


# ---------------------------------------------------------------- Gemini
def gemini_gw(http: httpx.AsyncClient) -> gemini.GeminiGateway:
    return gemini.GeminiGateway("AIza-test", client=http)


def gemini_ok(text: str = ANSWER_TEXT, reason: str = "STOP", **extra: Any) -> dict[str, Any]:
    return {
        "responseId": "gem_1",
        "candidates": [
            {
                "content": {"parts": [{"text": "thinking out loud", "thought": True}, {"text": text}]},
                "finishReason": reason,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 900,
            "cachedContentTokenCount": 100,
            "candidatesTokenCount": 50,
            "thoughtsTokenCount": 150,
        },
        **extra,
    }


def test_gemini_sends_the_schema_and_counts_thoughts_as_output() -> None:
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return httpx.Response(200, json=gemini_ok())

    result = run(gemini_gw, handler, "gemini", effort="low")
    assert result.output == ANSWER and result.provider_request_id == "gem_1"
    assert result.usage == Usage(input_tokens=800, output_tokens=200, cache_read_tokens=100)
    sent = seen[0]
    assert (
        str(sent.url) == "https://generativelanguage.googleapis.com/v1beta/models/test-model:generateContent"
    )
    assert sent.headers["x-goog-api-key"] == "AIza-test" and "key=" not in str(sent.url)
    config = json.loads(sent.content)["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"]["properties"] == {"summary": {"type": "string"}}
    assert config["maxOutputTokens"] == 4096 and config["thinkingConfig"] == {"thinkingLevel": "low"}


@pytest.mark.parametrize(
    ("body", "kind", "code"),
    [
        (gemini_ok(reason="MAX_TOKENS"), "invalid", "output_truncated"),
        (gemini_ok(reason="SAFETY"), "permanent", "provider_refusal"),
        (gemini_ok(reason="RECITATION"), "permanent", "provider_refusal"),
        (
            {**gemini_ok(), "candidates": [], "promptFeedback": {"blockReason": "SAFETY"}},
            "permanent",
            "provider_refusal",
        ),
        (gemini_ok(text="{"), "invalid", "output_not_json"),
    ],
)
def test_gemini_answers_that_are_not_usable(body: dict[str, Any], kind: str, code: str) -> None:
    error = failure(gemini_gw, lambda r: httpx.Response(200, json=body), "gemini")
    assert (error.kind, error.code) == (kind, code)


def test_gemini_reports_a_bad_key_as_auth() -> None:
    bad_key = {"error": {"status": "INVALID_ARGUMENT", "details": [{"reason": "API_KEY_INVALID"}]}}
    error = failure(gemini_gw, lambda r: httpx.Response(400, json=bad_key), "gemini")
    assert (error.kind, error.code) == ("permanent", "provider_auth")
    error = failure(
        gemini_gw, lambda r: httpx.Response(400, json={"error": {"status": "INVALID_ARGUMENT"}}), "gemini"
    )
    assert error.code == "provider_rejected"
    assert failure(gemini_gw, lambda r: httpx.Response(429, json={}), "gemini").kind == "transient"


# ---------------------------------------------------------------- OpenRouter
def openrouter_gw(http: httpx.AsyncClient) -> openrouter.OpenRouterGateway:
    return openrouter.OpenRouterGateway("sk-or-test", client=http)


def openrouter_ok(content: Any = ANSWER_TEXT, reason: str = "stop", **extra: Any) -> dict[str, Any]:
    return {
        "id": "gen-1",
        "model": "test-model",
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": reason}],
        "usage": {
            "prompt_tokens": 500,
            "completion_tokens": 80,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
        **extra,
    }


def test_openrouter_pins_the_model_price_and_data_policy() -> None:
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return httpx.Response(200, json=openrouter_ok())

    result = run(openrouter_gw, handler, "openrouter", effort="medium")
    assert result.output == ANSWER and result.provider_request_id == "gen-1"
    assert result.usage == Usage(input_tokens=500, output_tokens=80)
    sent = seen[0]
    assert str(sent.url) == "https://openrouter.ai/api/v1/chat/completions"
    body = json.loads(sent.content)
    assert "models" not in body  # never a model fallback
    assert body["provider"] == {
        "require_parameters": True,
        "data_collection": "deny",
        "max_price": {"prompt": 2.5, "completion": 10.0},  # the catalog price, USD per million tokens
    }
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["max_completion_tokens"] == 4096 and body["reasoning"] == {"effort": "medium"}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_openrouter_reads_content_parts() -> None:
    parts = [{"type": "text", "text": '{"summary": '}, {"type": "text", "text": '"Adds two numbers."}'}]
    assert (
        run(openrouter_gw, lambda r: httpx.Response(200, json=openrouter_ok(parts)), "openrouter").output
        == ANSWER
    )


@pytest.mark.parametrize(
    ("status", "body", "kind", "code"),
    [
        (200, openrouter_ok(reason="length"), "invalid", "output_truncated"),
        (200, openrouter_ok(reason="content_filter"), "permanent", "provider_refusal"),
        (200, openrouter_ok(reason="error"), "unknown", "provider_error"),
        (200, {"error": {"code": 502, "message": "upstream"}}, "transient", "provider_unavailable"),
        (200, {"error": {"code": 429, "message": "slow"}}, "transient", "provider_rate_limited"),
        (401, {"error": {"code": 401}}, "permanent", "provider_auth"),
        (402, {"error": {"code": 402}}, "permanent", "provider_quota"),
        (
            402,
            {"error": {"code": 402, "metadata": {"limit_source": "openrouter_in_flight_budget"}}},
            "transient",
            "provider_rate_limited",
        ),
        (
            403,
            {"error": {"code": 403, "metadata": {"reasons": ["flagged"]}}},
            "permanent",
            "provider_rejected",
        ),
        (503, {"error": {"code": 503}}, "transient", "provider_unavailable"),
    ],
)
def test_openrouter_failures_including_errors_inside_a_200(
    status: int, body: dict[str, Any], kind: str, code: str
) -> None:
    error = failure(openrouter_gw, lambda r: httpx.Response(status, json=body), "openrouter")
    assert (error.kind, error.code) == (kind, code)


# ---------------------------------------------------------------- catalog, settings, runtime, keys
def test_the_catalog_takes_batch_prices_for_providers_with_a_batch_api_only() -> None:
    price = {"input_per_mtok_micro_usd": 1, "output_per_mtok_micro_usd": 1}
    for provider in ("openai", "gemini"):
        model = request(provider).model.model_dump()
        assert Model.model_validate({**model, "batch_price": price}).batch_price is not None
    with pytest.raises(ValidationError, match="batches run only for anthropic, gemini, openai"):
        Model.model_validate({**request("openrouter").model.model_dump(), "batch_price": price})


def test_gemini_presets_cannot_ask_for_max_effort() -> None:
    model = request("gemini").model

    def preset(effort: str) -> catalog.Catalog:
        return catalog.Catalog.model_validate(
            {
                "revision": "catalog-2026-11-b",
                "models": [model.model_dump()],
                "presets": [
                    {
                        "name": "gemini-v1",
                        "description": "Gemini",
                        "writer": {"model": model.id, "max_output_tokens": 4096, "effort": effort},
                        "checker": {"model": model.id, "max_output_tokens": 2048},
                    }
                ],
            }
        )

    assert preset("xhigh").preset("gemini-v1") is not None
    with pytest.raises(ValidationError, match="gemini has no effort max"):
        preset("max")


def test_each_operator_key_enables_its_provider_and_only_openai_takes_a_base_url() -> None:
    settings = make_settings(
        openai_api_key=SecretStr("sk-op"),
        openai_base_url="https://llm-gateway.example.test/openai/v1/",
        gemini_api_key=SecretStr("AIza-op"),
        openrouter_api_key=SecretStr("  "),
    )
    assert settings.configured_providers() == {"replay", "openai", "gemini"}  # a blank key is unset
    assert settings.openai_base_url == "https://llm-gateway.example.test/openai/v1"
    with pytest.raises(ValidationError, match="OPENAI_BASE_URL must be an https URL"):
        make_settings(openai_base_url="http://plain.example.test")

    async def go() -> None:
        async with httpx.AsyncClient() as http:
            gateways = operator_gateways(settings, http)
            assert set(gateways) == {"replay", "openai", "gemini"}
            assert gateways["openai"].base_url == "https://llm-gateway.example.test/openai/v1"  # type: ignore[attr-defined]
            own = own_key_gateways(http)
            assert own("openai", "sk-ws").base_url == openai.OFFICIAL_BASE_URL  # type: ignore[attr-defined]
            assert isinstance(own("gemini", "k"), gemini.GeminiGateway)
            assert isinstance(own("openrouter", "k"), openrouter.OpenRouterGateway)

    asyncio.run(go())


@pytest.mark.parametrize(
    ("module", "url", "refused"),
    [
        (openai, "https://api.openai.com/v1/models", httpx.Response(401, json={})),
        (
            gemini,
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
            httpx.Response(400, json={"error": {"details": [{"reason": "API_KEY_INVALID"}]}}),
        ),
        (openrouter, "https://openrouter.ai/api/v1/key", httpx.Response(401, json={})),
    ],
)
def test_key_checks_go_to_the_official_api_and_raise_on_outages(
    module: Any, url: str, refused: httpx.Response
) -> None:
    seen: list[str] = []
    answers = iter([httpx.Response(200, json={"data": {}}), refused, httpx.Response(503)])

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(str(r.url))
        return next(answers)

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            assert await module.verify_key(http, "k" * 24) is True
            assert await module.verify_key(http, "k" * 24) is False
            with pytest.raises(httpx.HTTPStatusError):
                await module.verify_key(http, "k" * 24)

    asyncio.run(go())
    assert seen == [url] * 3


def test_openrouter_refuses_management_keys() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"is_management_key": True}})

    async def go() -> bool:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await openrouter.verify_key(http, "k" * 24)

    assert asyncio.run(go()) is False


def test_every_key_provider_has_a_gateway_and_a_check() -> None:
    assert keys.PROVIDERS == ("anthropic", "openai", "gemini", "openrouter")
    assert set(keys.PROVIDERS) | {"replay"} == set(Model.model_fields["provider"].annotation.__args__)  # type: ignore[union-attr]


def test_calls_wait_five_minutes_for_an_answer_and_fifteen_seconds_to_connect() -> None:
    """A reasoning model's large check can take over 90 s; a call cut off by its bound has no answer and
    keeps its hold, so the bound is 300 s. A connection that does not open fails fast (04)."""
    seen: list[dict[str, float]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.extensions["timeout"])
        return httpx.Response(200, json=openai_ok())

    run(openai_gw, handler, "openai")
    assert seen == [{"connect": 15.0, "read": 300.0, "write": 300.0, "pool": 300.0}]
    timeout = AnthropicGateway("sk-ant-test").client.timeout
    assert (timeout.connect, timeout.read) == (15.0, 300.0)  # type: ignore[union-attr]
