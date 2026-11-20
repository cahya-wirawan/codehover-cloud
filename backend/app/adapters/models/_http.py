"""Shared parts of the raw-HTTP model adapters (OpenAI, Gemini, OpenRouter; 07 "Provider abstraction").

These adapters use httpx directly, like the Stripe adapter, rather than vendor SDKs:
- Every request names its full URL, so no SDK fallback (an `OPENAI_BASE_URL` environment variable, a
  credentials file) can redirect a call.
- The runtime's one shared client is used. It never follows redirects, and a call is bounded at 90 s (04).
- There are no hidden retries: retries are persisted, budgeted attempts.

Failures are normalized into `ProviderError` kinds here. A connection that never opened was not billed, so
it is transient. A call that may have reached the provider is `unknown` and keeps its hold. Response
bodies and error text never leave the adapter, because they can echo the request.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.engine.gateway import GenerationRequest, GenerationResult, ProviderError, Usage

CALL_TIMEOUT_SECONDS = 90.0
VERIFY_TIMEOUT_SECONDS = 15.0


def retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def error_body(response: httpx.Response) -> dict[str, Any]:
    """The provider's `error` object, or {} when the body is not the documented JSON shape."""
    try:
        body = response.json()
    except ValueError:
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


def status_error(
    response: httpx.Response, request_id: str | None, status: int | None = None
) -> ProviderError:
    """The default meaning of an HTTP error status (the response's, or `status` for an error reported in
    the body); adapters refine provider-specific cases first."""
    status = response.status_code if status is None else status
    if status in (401, 403):
        return ProviderError("permanent", "provider_auth", provider_request_id=request_id)
    if status == 408:  # the provider gave up while it may already have been working
        return ProviderError("unknown", "provider_timeout", provider_request_id=request_id)
    if status == 429:
        return ProviderError(
            "transient",
            "provider_rate_limited",
            provider_request_id=request_id,
            retry_after_seconds=retry_after(response),
        )
    if status >= 500:
        return ProviderError(
            "transient",
            "provider_unavailable",
            provider_request_id=request_id,
            retry_after_seconds=retry_after(response),
        )
    return ProviderError("permanent", "provider_rejected", provider_request_id=request_id)


async def post(
    client: httpx.AsyncClient, url: str, *, headers: dict[str, str], body: dict[str, Any]
) -> httpx.Response:
    try:
        return await client.post(url, json=body, headers=headers, timeout=CALL_TIMEOUT_SECONDS)
    except (httpx.ConnectError, httpx.ConnectTimeout) as error:  # nothing was sent, so nothing was billed
        raise ProviderError("transient", "provider_connection") from error
    except httpx.TimeoutException as error:
        raise ProviderError("unknown", "provider_timeout") from error
    except httpx.TransportError as error:
        raise ProviderError("unknown", "provider_connection") from error


def json_body(response: httpx.Response, request_id: str | None) -> dict[str, Any]:
    """A successful response's body. A 200 that is not JSON may still have been billed."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise ProviderError("unknown", "provider_malformed", provider_request_id=request_id)
    return body


def count(value: Any) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def result(
    text: str | None, request: GenerationRequest, request_id: str | None, usage: Usage
) -> GenerationResult:
    try:
        output = json.loads(text) if text is not None else None
    except ValueError:
        output = None
    if not isinstance(output, dict):
        raise ProviderError("invalid", "output_not_json", provider_request_id=request_id, usage=usage)
    return GenerationResult(
        output=output, usage=usage, provider_request_id=request_id, model_id=request.model.id
    )


async def key_accepted(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], *, refused: tuple[int, ...] = (401, 403)
) -> bool:
    """One cheap authenticated GET: True when accepted, False when the key is refused. Any other outcome
    raises, so an outage never saves (or rejects) a key."""
    response = await client.get(url, headers=headers, timeout=VERIFY_TIMEOUT_SECONDS)
    if response.status_code in refused:
        return False
    response.raise_for_status()
    return True
