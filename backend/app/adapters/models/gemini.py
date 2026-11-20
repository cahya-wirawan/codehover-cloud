"""Google Gemini API adapter (07 "Provider abstraction"): `generateContent` with an API key, over httpx (see
`_http`). This is the Gemini API, not Vertex AI.

- Structured output uses `generationConfig.responseMimeType` "application/json" with `responseJsonSchema`,
  set to the stage schema's provider projection (standard JSON Schema, objects closed). The answer is the
  text of the first candidate's parts, without thought parts.
- `maxOutputTokens` bounds the answer. Thinking tokens are billed as output, so they count as output
  usage. `effort` maps to `thinkingConfig.thinkingLevel`, which Gemini accepts as low, medium, high or
  xhigh depending on the model; the catalog rejects `max` for Gemini models.
- The endpoint is always the official API, with the key in the `x-goog-api-key` header, never in the URL.
- A blocked prompt or a safety, recitation or policy stop is a refusal: permanent for this call.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from app.adapters.models import _http
from app.engine.gateway import GenerationRequest, GenerationResult, ProviderError, Usage

OFFICIAL_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
REFUSALS = frozenset(
    {
        "SAFETY",
        "RECITATION",
        "LANGUAGE",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
    }
)


def _usage(raw: Any) -> Usage:
    raw = raw if isinstance(raw, dict) else {}
    cached = _http.count(raw.get("cachedContentTokenCount"))
    return Usage(  # promptTokenCount includes the cached part; thoughts are billed as output
        input_tokens=max(0, _http.count(raw.get("promptTokenCount")) - cached),
        output_tokens=_http.count(raw.get("candidatesTokenCount"))
        + _http.count(raw.get("thoughtsTokenCount")),
        cache_read_tokens=cached,
    )


def _key_refused(response: httpx.Response) -> bool:
    """Gemini answers a bad key with 400 INVALID_ARGUMENT and reason API_KEY_INVALID."""
    details = _http.error_body(response).get("details")
    return any(isinstance(d, dict) and d.get("reason") == "API_KEY_INVALID" for d in details or [])


class GeminiGateway:
    def __init__(self, api_key: str, *, client: httpx.AsyncClient) -> None:
        self.client = client
        self._headers = {"x-goog-api-key": api_key}

    def body(self, request: GenerationRequest) -> dict[str, Any]:
        config: dict[str, Any] = {
            "responseMimeType": "application/json",
            "responseJsonSchema": request.provider_schema(),
            "maxOutputTokens": request.max_output_tokens,
        }
        if request.role.effort is not None:
            config["thinkingConfig"] = {"thinkingLevel": request.role.effort}
        return {
            "systemInstruction": {"parts": [{"text": request.system}]},
            "contents": [{"role": "user", "parts": [{"text": request.user}]}],
            "generationConfig": config,
        }

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        model = quote(request.model.provider_model, safe="")
        response = await _http.post(
            self.client,
            f"{OFFICIAL_BASE_URL}/models/{model}:generateContent",
            headers=self._headers,
            body=self.body(request),
        )
        if response.status_code != 200:
            if response.status_code == 400 and _key_refused(response):
                raise ProviderError("permanent", "provider_auth")
            raise _http.status_error(response, None)
        body = _http.json_body(response, None)
        request_id = body.get("responseId") if isinstance(body.get("responseId"), str) else None
        usage = _usage(body.get("usageMetadata"))
        if (body.get("promptFeedback") or {}).get("blockReason"):
            raise ProviderError("permanent", "provider_refusal", provider_request_id=request_id, usage=usage)
        candidates = body.get("candidates") or []
        candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
        reason = candidate.get("finishReason")
        if reason in REFUSALS:
            raise ProviderError("permanent", "provider_refusal", provider_request_id=request_id, usage=usage)
        if reason == "MAX_TOKENS":
            raise ProviderError("invalid", "output_truncated", provider_request_id=request_id, usage=usage)
        parts = (candidate.get("content") or {}).get("parts") or []
        texts = [
            p["text"]
            for p in parts
            if isinstance(p, dict) and not p.get("thought") and isinstance(p.get("text"), str)
        ]
        return _http.result("".join(texts) if texts else None, request, request_id, usage)


async def verify_key(client: httpx.AsyncClient, key: str) -> bool:
    response = await client.get(
        f"{OFFICIAL_BASE_URL}/models",
        params={"pageSize": 1},
        headers={"x-goog-api-key": key},
        timeout=_http.VERIFY_TIMEOUT_SECONDS,
    )
    if response.status_code in (401, 403) or (response.status_code == 400 and _key_refused(response)):
        return False
    response.raise_for_status()
    return True
