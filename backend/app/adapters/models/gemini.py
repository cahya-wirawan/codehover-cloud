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
- Batches (07, T24): Batch Mode with inline requests (`models/{model}:batchGenerateContent`, under its
  20 MB limit; a larger batch is not sent and the stage runs synchronously). Each request carries its
  attempt ID as `metadata.key`; results are matched by that key, or by position, which the API keeps.
  The job's state and its inlined responses are read where the API reports them (the operation's
  `metadata` or `response`, or the batch resource itself). Requests a failed, cancelled or expired job
  never answered are errors and are not billed.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

import httpx

from app.adapters.models import _http
from app.engine.gateway import BatchItem, GenerationRequest, GenerationResult, ProviderError, Usage

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


MAX_INLINE_BYTES = 19_000_000  # the API takes inline batches under 20 MB
SUCCEEDED = frozenset({"JOB_STATE_SUCCEEDED", "BATCH_STATE_SUCCEEDED"})
UNFINISHED = frozenset(
    {
        "JOB_STATE_FAILED",
        "JOB_STATE_CANCELLED",
        "JOB_STATE_EXPIRED",
        "BATCH_STATE_FAILED",
        "BATCH_STATE_CANCELLED",
        "BATCH_STATE_EXPIRED",
    }
)
BATCH_NAME = re.compile(r"^(batches|operations)/[A-Za-z0-9_-]{1,200}$")


def _dig(value: Any, *path: str) -> Any:
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _state(body: dict[str, Any]) -> str | None:
    state = _dig(body, "metadata", "state") or body.get("state")
    return state if isinstance(state, str) else None


def _inlined(body: dict[str, Any]) -> list[Any]:
    for path in (
        ("response", "inlinedResponses", "inlinedResponses"),
        ("response", "output", "inlinedResponses", "inlinedResponses"),
        ("metadata", "output", "inlinedResponses", "inlinedResponses"),
        ("output", "inlinedResponses", "inlinedResponses"),
        ("dest", "inlinedResponses"),
    ):
        found = _dig(body, *path)
        if isinstance(found, list):
            return found
    return []


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
        return _answer(_http.json_body(response, None), request)

    async def submit_batch(self, items: list[tuple[str, GenerationRequest]]) -> str:
        model = items[0][1].model.provider_model
        body = {
            "batch": {
                "displayName": f"codehover-{items[0][0]}"[:120],
                "model": f"models/{model}",
                "inputConfig": {
                    "requests": {
                        "requests": [{"request": self.body(r), "metadata": {"key": cid}} for cid, r in items]
                    }
                },
            }
        }
        payload = json.dumps(body).encode()
        if len(payload) > MAX_INLINE_BYTES:
            raise ProviderError("permanent", _http.NOT_SENT)
        response = await _http.batch_call(
            self.client,
            "POST",
            f"{OFFICIAL_BASE_URL}/models/{quote(model, safe='')}:batchGenerateContent",
            headers={**self._headers, "Content-Type": "application/json"},
            code="batch_submit_failed",
            unsent_code=_http.NOT_SENT,
            content=payload,
        )
        try:
            name = response.json().get("name")
        except (ValueError, AttributeError):
            name = None
        if not isinstance(name, str) or not BATCH_NAME.match(name):
            raise ProviderError("transient", "batch_submit_failed")
        return name

    async def _batch(self, name: str) -> dict[str, Any]:
        if not BATCH_NAME.match(name):
            raise ProviderError("transient", "batch_status_failed")
        response = await _http.batch_call(
            self.client,
            "GET",
            f"{OFFICIAL_BASE_URL}/{name}",
            headers=self._headers,
            code="batch_status_failed",
        )
        body = response.json()
        return body if isinstance(body, dict) else {}

    async def batch_ended(self, batch_id: str) -> bool:
        body = await self._batch(batch_id)
        return body.get("done") is True or _state(body) in SUCCEEDED | UNFINISHED

    async def batch_results(self, batch_id: str, requests: dict[str, GenerationRequest]) -> list[BatchItem]:
        body = await self._batch(batch_id)
        inlined = _inlined(body)
        order = list(requests) if len(inlined) == len(requests) else []
        found: dict[str, BatchItem] = {}
        for index, entry in enumerate(inlined):
            if not isinstance(entry, dict):
                continue
            key = _dig(entry, "metadata", "key")
            cid = key if isinstance(key, str) else (order[index] if order else None)
            if cid is None or cid not in requests or cid in found:
                continue  # not one of ours: never attach it to a request
            answer = entry.get("response")
            if isinstance(answer, dict):
                try:
                    found[cid] = BatchItem(cid, _answer(answer, requests[cid]))
                except ProviderError as error:
                    found[cid] = BatchItem(cid, None, error)
            else:
                found[cid] = BatchItem(cid, None, ProviderError("permanent", "batch_request_failed"))
        state = _state(body)
        if state in UNFINISHED or (body.get("done") is True and body.get("error")):  # never ran: not billed
            for cid in requests:
                found.setdefault(cid, BatchItem(cid, None, ProviderError("permanent", "batch_failed")))
        return list(found.values())


def _answer(body: dict[str, Any], request: GenerationRequest) -> GenerationResult:
    """A GenerateContentResponse as the stage's answer, the same for a synchronous call and a batch result."""
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
