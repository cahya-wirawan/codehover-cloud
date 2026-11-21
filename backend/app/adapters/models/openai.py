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
- Batches (07, T24): the requests go up as one JSONL file (`purpose=batch`), the batch runs on
  `/v1/responses` within the 24-hour window, and each answer in the output file is read exactly like a
  synchronous response. Requests the error file lists, or that a failed, expired or cancelled batch never
  answered, are errors and are not billed.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.adapters.models import _http
from app.engine.gateway import BatchItem, GenerationRequest, GenerationResult, ProviderError, Usage

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


BATCH_ENDPOINT = "/v1/responses"
ENDED = frozenset({"completed", "failed", "expired", "cancelled"})
UNFINISHED = frozenset({"failed", "expired", "cancelled"})  # requests without a line here never ran


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
        return _answer(_http.json_body(response, request_id), request, request_id)

    async def submit_batch(self, items: list[tuple[str, GenerationRequest]]) -> str:
        lines = "".join(
            json.dumps({"custom_id": cid, "method": "POST", "url": BATCH_ENDPOINT, "body": self.body(r)})
            + "\n"
            for cid, r in items
        )
        upload = await _http.batch_call(
            self.client,
            "POST",
            f"{self.base_url}/files",
            headers=self._headers,
            code=_http.NOT_SENT,  # no batch exists until the create call below
            data={"purpose": "batch"},
            files={"file": ("requests.jsonl", lines.encode(), "application/jsonl")},
        )
        file_id = _string(upload, "id")
        created = await _http.batch_call(
            self.client,
            "POST",
            f"{self.base_url}/batches",
            headers=self._headers,
            code="batch_submit_failed",
            unsent_code=_http.NOT_SENT,
            json={"input_file_id": file_id, "endpoint": BATCH_ENDPOINT, "completion_window": "24h"},
        )
        return _string(created, "id")

    async def _batch(self, batch_id: str) -> dict[str, Any]:
        response = await _http.batch_call(
            self.client,
            "GET",
            f"{self.base_url}/batches/{_safe_id(batch_id)}",
            headers=self._headers,
            code="batch_status_failed",
        )
        body = response.json()
        return body if isinstance(body, dict) else {}

    async def batch_ended(self, batch_id: str) -> bool:
        return (await self._batch(batch_id)).get("status") in ENDED

    async def batch_results(self, batch_id: str, requests: dict[str, GenerationRequest]) -> list[BatchItem]:
        batch = await self._batch(batch_id)
        found: dict[str, BatchItem] = {}
        for field in ("output_file_id", "error_file_id"):
            file_id = batch.get(field)
            if not isinstance(file_id, str) or not file_id:
                continue
            content = await _http.batch_call(
                self.client,
                "GET",
                f"{self.base_url}/files/{_safe_id(file_id)}/content",
                headers=self._headers,
                code="batch_results_failed",
            )
            for raw in content.text.splitlines():
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue
                cid = line.get("custom_id") if isinstance(line, dict) else None
                if not isinstance(cid, str) or cid not in requests or cid in found:
                    continue  # not one of ours: never attach it to a request
                request = requests[cid]
                response = line.get("response")
                if isinstance(response, dict) and response.get("status_code") == 200:
                    body = response.get("body")
                    request_id = (
                        response.get("request_id") if isinstance(response.get("request_id"), str) else None
                    )
                    try:
                        found[cid] = BatchItem(
                            cid, _answer(body if isinstance(body, dict) else {}, request, request_id)
                        )
                    except ProviderError as error:
                        found[cid] = BatchItem(cid, None, error)
                else:
                    expired = (line.get("error") or {}).get("code") == "batch_expired"
                    code = "batch_expired" if expired else "batch_request_failed"
                    found[cid] = BatchItem(cid, None, ProviderError("permanent", code))
        if batch.get("status") in UNFINISHED:  # what the batch never ran is not billed
            for cid in requests:
                found.setdefault(
                    cid, BatchItem(cid, None, ProviderError("permanent", f"batch_{batch['status']}"))
                )
        return list(found.values())


def _string(response: httpx.Response, field: str) -> str:
    """A required string field of a batch API answer; anything else is a failed call."""
    try:
        value = response.json().get(field)
    except (ValueError, AttributeError):
        value = None
    if not isinstance(value, str) or not value:
        raise ProviderError("transient", "batch_submit_failed")
    return value


def _safe_id(value: str) -> str:
    """A provider ID used as one URL path segment."""
    if not value.replace("_", "").replace("-", "").isalnum():
        raise ProviderError("transient", "batch_status_failed")
    return value


def _answer(body: dict[str, Any], request: GenerationRequest, request_id: str | None) -> GenerationResult:
    """A Responses API body as the stage's answer, the same for a synchronous call and a batch line."""
    request_id = request_id or (body.get("id") if isinstance(body.get("id"), str) else None)
    usage = _usage(body.get("usage"))
    status = body.get("status")
    if status == "incomplete":
        reason = (body.get("incomplete_details") or {}).get("reason")
        if reason == "content_filter":
            raise ProviderError("permanent", "provider_refusal", provider_request_id=request_id, usage=usage)
        raise ProviderError("invalid", "output_truncated", provider_request_id=request_id, usage=usage)
    if status != "completed":  # failed, or a state a synchronous call should never return
        raise ProviderError("transient", "provider_unavailable", provider_request_id=request_id, usage=usage)
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
