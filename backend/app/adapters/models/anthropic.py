"""Anthropic Messages API adapter (07 "Provider abstraction"), through the official `anthropic` SDK.

- Structured output: `output_config.format` with the provider projection of the stage schema; the
  answer is the first text block and is parsed and validated by the caller.
- No SDK retries (`max_retries=0`): retries are persisted, budgeted attempts (04), never hidden ones.
- Bounded call time (90 s, 04). The endpoint is the SDK default; hosted mode never accepts a
  user-supplied base URL (07).
- No server-side model fallback: every model that serves a request must be an approved, priced catalog
  model, so a refusal is a permanent error for this call rather than a silent switch to another model.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic
from anthropic.types import OutputConfigParam, UserMessageParam
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from app.engine.gateway import BatchItem, GenerationRequest, GenerationResult, ProviderError, Usage

CALL_TIMEOUT_SECONDS = 90.0


def _usage(raw: Any) -> Usage:
    return Usage(
        input_tokens=int(getattr(raw, "input_tokens", 0) or 0),
        output_tokens=int(getattr(raw, "output_tokens", 0) or 0),
        cache_read_tokens=int(getattr(raw, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(raw, "cache_creation_input_tokens", 0) or 0),
    )


def _retry_after(error: anthropic.APIStatusError) -> float | None:
    value = error.response.headers.get("retry-after") if error.response is not None else None
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


class AnthropicGateway:
    def __init__(self, api_key: str, *, client: anthropic.AsyncAnthropic | None = None) -> None:
        self.client = client or anthropic.AsyncAnthropic(
            api_key=api_key, max_retries=0, timeout=CALL_TIMEOUT_SECONDS
        )

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        output_config: OutputConfigParam = {
            "format": {"type": "json_schema", "schema": request.provider_schema()}
        }
        if request.role.effort is not None:
            output_config["effort"] = request.role.effort
        user: UserMessageParam = {"role": "user", "content": request.user}
        try:
            message = await self.client.messages.create(
                model=request.model.provider_model,
                max_tokens=request.max_output_tokens,
                system=request.system,
                messages=[user],
                output_config=output_config,
            )
        except anthropic.APITimeoutError as error:
            raise ProviderError("unknown", "provider_timeout") from error
        except anthropic.APIConnectionError as error:
            raise ProviderError("unknown", "provider_connection") from error
        except anthropic.DeadlineExceededError as error:
            raise ProviderError(
                "unknown", "provider_deadline", provider_request_id=error.request_id
            ) from error
        except (anthropic.RateLimitError, anthropic.OverloadedError) as error:
            raise ProviderError(
                "transient",
                "provider_rate_limited",
                provider_request_id=error.request_id,
                retry_after_seconds=_retry_after(error),
            ) from error
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as error:
            raise ProviderError("permanent", "provider_auth", provider_request_id=error.request_id) from error
        except (
            anthropic.BadRequestError,
            anthropic.NotFoundError,
            anthropic.RequestTooLargeError,
            anthropic.UnprocessableEntityError,
        ) as error:
            raise ProviderError(
                "permanent", "provider_rejected", provider_request_id=error.request_id
            ) from error
        except anthropic.APIStatusError as error:
            if error.status_code >= 500:
                raise ProviderError(
                    "transient",
                    "provider_unavailable",
                    provider_request_id=error.request_id,
                    retry_after_seconds=_retry_after(error),
                ) from error
            raise ProviderError(
                "permanent", "provider_rejected", provider_request_id=error.request_id
            ) from error

        return _result(message, request, getattr(message, "_request_id", None))

    # ---------------------------------------------------------------- Message Batches (M6, T24)
    def _params(self, request: GenerationRequest) -> MessageCreateParamsNonStreaming:
        output_config: OutputConfigParam = {
            "format": {"type": "json_schema", "schema": request.provider_schema()}
        }
        if request.role.effort is not None:
            output_config["effort"] = request.role.effort
        return {
            "model": request.model.provider_model,
            "max_tokens": request.max_output_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": request.user}],
            "output_config": output_config,
        }

    async def submit_batch(self, items: list[tuple[str, GenerationRequest]]) -> str:
        try:
            batch = await self.client.messages.batches.create(
                requests=[Request(custom_id=cid, params=self._params(r)) for cid, r in items]
            )
        except anthropic.APIError as error:
            raise ProviderError("transient", "batch_submit_failed") from error
        return str(batch.id)

    async def batch_ended(self, batch_id: str) -> bool:
        try:
            batch = await self.client.messages.batches.retrieve(batch_id)
        except anthropic.APIError as error:
            raise ProviderError("transient", "batch_status_failed") from error
        return batch.processing_status == "ended"

    async def batch_results(self, batch_id: str, requests: dict[str, GenerationRequest]) -> list[BatchItem]:
        items: list[BatchItem] = []
        try:
            async for entry in await self.client.messages.batches.results(batch_id):
                request = requests.get(entry.custom_id)
                if request is None:
                    continue  # not one of ours: never attach it to a request
                if entry.result.type == "succeeded":
                    try:
                        items.append(BatchItem(entry.custom_id, _result(entry.result.message, request, None)))
                    except ProviderError as error:
                        items.append(BatchItem(entry.custom_id, None, error))
                else:  # errored, canceled or expired: not billed by the provider
                    items.append(
                        BatchItem(
                            entry.custom_id, None, ProviderError("permanent", f"batch_{entry.result.type}")
                        )
                    )
        except anthropic.APIError as error:
            raise ProviderError("transient", "batch_results_failed") from error
        return items


def _result(message: Any, request: GenerationRequest, request_id: str | None) -> GenerationResult:
    usage = _usage(message.usage)
    if message.stop_reason == "refusal":
        raise ProviderError("permanent", "provider_refusal", provider_request_id=request_id, usage=usage)
    if message.stop_reason == "max_tokens":
        raise ProviderError("invalid", "output_truncated", provider_request_id=request_id, usage=usage)
    text = next((block.text for block in message.content if block.type == "text"), None)
    try:
        output = json.loads(text) if text is not None else None
    except ValueError:
        output = None
    if not isinstance(output, dict):
        raise ProviderError("invalid", "output_not_json", provider_request_id=request_id, usage=usage)
    return GenerationResult(
        output=output, usage=usage, provider_request_id=request_id, model_id=request.model.id
    )
