"""OpenRouter adapter (07 "Provider abstraction"): chat completions, over httpx (see `_http`).

OpenRouter routes one model to one of several hosting providers, so the request pins down what the
catalog approved:
- No model fallback. `models` is never sent, so only the catalog model can answer.
- `provider.max_price` is set to the catalog price. OpenRouter will not route to a host that charges more,
  so the ledger's charge at the catalog price is never below the real bill.
- `provider.data_collection: "deny"` excludes hosts that may store or train on prompts.
- `provider.require_parameters: true` routes only to hosts that support strict `response_format`.
- `max_completion_tokens` bounds the answer and reasoning together, and reasoning is billed as output.
  `effort` maps to `reasoning.effort`.

Errors can arrive inside a 200: a top-level `error`, or a choice that stopped with `error`. Both are
checked before the content is read. The endpoint is always the official API.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.adapters.models import _http
from app.engine.catalog import MILLION, Price
from app.engine.gateway import GenerationRequest, GenerationResult, ProviderError, Usage

OFFICIAL_BASE_URL = "https://openrouter.ai/api/v1"


def _usage(raw: Any) -> Usage:
    raw = raw if isinstance(raw, dict) else {}
    details = raw.get("prompt_tokens_details") or {}
    cached = _http.count(details.get("cached_tokens"))
    return Usage(
        input_tokens=max(0, _http.count(raw.get("prompt_tokens")) - cached),
        output_tokens=_http.count(raw.get("completion_tokens")),
        cache_read_tokens=cached,
    )


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _usd_per_mtok(micro_usd: int) -> float:
    return micro_usd / MILLION  # OpenRouter's max_price is in USD per million tokens


def max_price(price: Price) -> dict[str, float]:
    return {
        "prompt": _usd_per_mtok(price.input_per_mtok_micro_usd),
        "completion": _usd_per_mtok(price.output_per_mtok_micro_usd),
    }


def _error(
    status: int, error: dict[str, Any], request_id: str | None, response: httpx.Response
) -> ProviderError:
    metadata = _object(error.get("metadata"))
    if status == 402:  # out of credits: waiting helps only while other requests still hold credit
        if metadata.get("limit_source") == "openrouter_in_flight_budget":
            return ProviderError(
                "transient",
                "provider_rate_limited",
                provider_request_id=request_id,
                retry_after_seconds=_http.retry_after(response),
            )
        return ProviderError("permanent", "provider_quota", provider_request_id=request_id)
    if status == 403:  # moderation, a guardrail or a spent key limit; never a retry
        return ProviderError("permanent", "provider_rejected", provider_request_id=request_id)
    return _http.status_error(response, request_id, status)


class OpenRouterGateway:
    def __init__(self, api_key: str, *, client: httpx.AsyncClient) -> None:
        self.client = client
        self._headers = {"Authorization": f"Bearer {api_key}"}

    def body(self, request: GenerationRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model.provider_model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "max_completion_tokens": request.max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": request.purpose, "strict": True, "schema": request.provider_schema()},
            },
            "provider": {
                "require_parameters": True,
                "data_collection": "deny",
                "max_price": max_price(request.model.price),
            },
        }
        if request.role.effort is not None:
            body["reasoning"] = {"effort": request.role.effort}
        return body

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        response = await _http.post(
            self.client,
            f"{OFFICIAL_BASE_URL}/chat/completions",
            headers=self._headers,
            body=self.body(request),
        )
        if response.status_code != 200:
            raise _error(response.status_code, _http.error_body(response), None, response)
        body = _http.json_body(response, None)
        request_id = body.get("id") if isinstance(body.get("id"), str) else None
        usage = _usage(body.get("usage"))
        if isinstance(body.get("error"), dict):  # failed before any choice was produced
            error = body["error"]
            code = error.get("code")
            failure = _error(code if isinstance(code, int) else 502, error, request_id, response)
            failure.usage = usage if usage != Usage() else None
            raise failure
        choices = body.get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        reason = choice.get("finish_reason")
        message = _object(choice.get("message"))
        if reason == "error":  # the host failed while generating: the prompt may have been billed
            raise ProviderError("unknown", "provider_error", provider_request_id=request_id, usage=usage)
        if reason == "content_filter" or message.get("refusal"):
            raise ProviderError("permanent", "provider_refusal", provider_request_id=request_id, usage=usage)
        if reason == "length":
            raise ProviderError("invalid", "output_truncated", provider_request_id=request_id, usage=usage)
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                p["text"] for p in content if isinstance(p, dict) and isinstance(p.get("text"), str)
            )
        return _http.result(
            content if isinstance(content, str) and content else None, request, request_id, usage
        )


async def verify_key(client: httpx.AsyncClient, key: str) -> bool:
    """A management key is accepted by `/key` but cannot run inference, so it is refused here."""
    response = await client.get(
        f"{OFFICIAL_BASE_URL}/key",
        headers={"Authorization": f"Bearer {key}"},
        timeout=_http.VERIFY_TIMEOUT_SECONDS,
    )
    if response.status_code in (401, 403):
        return False
    response.raise_for_status()
    data = response.json().get("data")
    return not (isinstance(data, dict) and data.get("is_management_key"))
