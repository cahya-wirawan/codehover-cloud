"""OpenAI Responses API adapter (07 "Provider abstraction"), over httpx (see `_http`).

- Structured output uses `text.format` with a strict `json_schema`, set to the stage schema's provider
  projection. Every stage schema already lists every property as required and closes every object, which
  strict mode demands. The answer is the text of the response's message items.
- `store: false`: no response is kept with the provider for later turns, because the pipeline never uses
  one.
- `max_output_tokens` bounds visible and reasoning tokens together, and reasoning is billed as output, so
  the earmark's output bound holds. `effort` maps to `reasoning.effort`.
- The endpoint is the official API, or the operator's `OPENAI_BASE_URL` setting for calls with the
  operator's key only. A workspace's own key always goes to the official API (08).
- No model fallback: the catalog model is the only one asked.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.adapters.models import _http
from app.engine.gateway import GenerationRequest, GenerationResult, ProviderError, Usage

OFFICIAL_BASE_URL = "https://api.openai.com/v1"
# 429s that mean the account cannot pay or has hit its limit: waiting does not help.
QUOTA_CODES = frozenset(
    {
        "insufficient_quota",
        "credit_balance_exhausted",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
    }
)


def _usage(raw: Any) -> Usage:
    raw = raw if isinstance(raw, dict) else {}
    details = raw.get("input_tokens_details") or {}
    cached = _http.count(details.get("cached_tokens"))
    return Usage(  # input_tokens includes the cached part; the ledger prices them apart
        input_tokens=max(0, _http.count(raw.get("input_tokens")) - cached),
        output_tokens=_http.count(raw.get("output_tokens")),
        cache_read_tokens=cached,
    )


class OpenAIGateway:
    def __init__(self, api_key: str, *, client: httpx.AsyncClient, base_url: str = OFFICIAL_BASE_URL) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}

    def body(self, request: GenerationRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model.provider_model,
            "instructions": request.system,
            "input": [{"role": "user", "content": request.user}],
            "max_output_tokens": request.max_output_tokens,
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": request.purpose,
                    "schema": request.provider_schema(),
                    "strict": True,
                }
            },
        }
        if request.role.effort is not None:
            body["reasoning"] = {"effort": request.role.effort}
        return body

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        response = await _http.post(
            self.client, f"{self.base_url}/responses", headers=self._headers, body=self.body(request)
        )
        request_id = response.headers.get("x-request-id")
        if response.status_code != 200:
            if response.status_code == 429 and _http.error_body(response).get("code") in QUOTA_CODES:
                raise ProviderError("permanent", "provider_quota", provider_request_id=request_id)
            raise _http.status_error(response, request_id)
        body = _http.json_body(response, request_id)
        request_id = request_id or (body.get("id") if isinstance(body.get("id"), str) else None)
        usage = _usage(body.get("usage"))
        status = body.get("status")
        if status == "incomplete":
            reason = (body.get("incomplete_details") or {}).get("reason")
            if reason == "content_filter":
                raise ProviderError(
                    "permanent", "provider_refusal", provider_request_id=request_id, usage=usage
                )
            raise ProviderError("invalid", "output_truncated", provider_request_id=request_id, usage=usage)
        if status != "completed":  # failed, or a state a synchronous call should never return
            raise ProviderError(
                "transient", "provider_unavailable", provider_request_id=request_id, usage=usage
            )
        texts: list[str] = []
        for item in body.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue  # reasoning summaries and other items carry no answer
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "refusal":
                    raise ProviderError(
                        "permanent", "provider_refusal", provider_request_id=request_id, usage=usage
                    )
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    texts.append(part["text"])
        return _http.result("".join(texts) if texts else None, request, request_id, usage)


async def verify_key(client: httpx.AsyncClient, key: str) -> bool:
    return await _http.key_accepted(client, f"{OFFICIAL_BASE_URL}/models", {"Authorization": f"Bearer {key}"})
