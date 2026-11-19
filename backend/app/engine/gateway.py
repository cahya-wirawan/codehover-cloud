"""Provider gateway contract (07 "Provider abstraction").

`ModelGateway.generate(request) -> GenerationResult`. The request carries the catalog model, the role
parameters, the stage purpose, the prompt-set version, the rendered prompt and the JSON schema the
answer must satisfy. The structured `evidence` is the data the prompt was rendered from; only the
deterministic replay gateway reads it. The result carries the provider request ID, actual usage
including cache tokens, the parsed output and its digest.

Failures are normalized into `ProviderError.kind`:
- transient: nothing was billed or the provider rejected before work (429, 5xx, overloaded); retry.
- permanent: the request cannot succeed as sent (auth, invalid request, refusal); do not retry.
- unknown: the outcome is uncertain (timeout, dropped connection); the spend may have happened, so the
  conservative hold stays until reconciliation (11).
- invalid: the provider answered, but the answer is not valid JSON for the schema (one repair call).

The gateway exposes generation only: no tools, shell, network or file access for the model (19 P08).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.engine.catalog import Model, Role

ErrorKind = Literal["transient", "permanent", "unknown", "invalid"]
# Keywords strict structured-output modes reject; the local validator enforces them instead (19 P02).
UNSUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "maxLength",
        "minLength",
        "maxItems",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "pattern",
        "format",
        "$schema",
        "$id",
        "$comment",
        "title",
        "uniqueItems",
    }
)
MESSAGE_OVERHEAD_TOKENS = 64


class ProviderError(Exception):
    def __init__(
        self,
        kind: ErrorKind,
        code: str,
        *,
        provider_request_id: str | None = None,
        retry_after_seconds: float | None = None,
        usage: Usage | None = None,
    ) -> None:
        super().__init__(code)
        self.kind = kind
        self.code = code
        self.provider_request_id = provider_request_id
        self.retry_after_seconds = retry_after_seconds
        self.usage = usage  # billed usage when the provider answered (invalid output, refusal)


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class GenerationRequest:
    model: Model
    role: Role
    purpose: str  # explain, overview, plan, guide, check, repair (provider_attempts.purpose)
    prompt_version: str
    system: str
    user: str
    schema: dict[str, Any]
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def max_output_tokens(self) -> int:
        return self.role.max_output_tokens

    def provider_schema(self) -> dict[str, Any]:
        return provider_schema(self.schema)

    def digest(self) -> str:
        body = {
            "model": self.model.id,
            "max_output_tokens": self.role.max_output_tokens,
            "effort": self.role.effort,
            "purpose": self.purpose,
            "prompts": self.prompt_version,
            "system": self.system,
            "user": self.user,
            "schema": self.schema,
        }
        return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def input_token_bound(self) -> int:
        """Upper bound for billed input tokens without a provider call: byte-level tokenizers never
        produce more tokens than UTF-8 bytes, plus per-message framing."""
        size = len(self.system.encode()) + len(self.user.encode())
        size += len(json.dumps(self.provider_schema(), separators=(",", ":")).encode())
        return size + MESSAGE_OVERHEAD_TOKENS


@dataclass(frozen=True)
class GenerationResult:
    output: dict[str, Any]
    usage: Usage
    provider_request_id: str | None
    model_id: str

    @property
    def output_digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.output, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class ModelGateway(Protocol):
    async def generate(self, request: GenerationRequest) -> GenerationResult: ...


@dataclass(frozen=True)
class BatchItem:
    """One answer of a finished batch: a result, or the error the provider reported for that request."""

    custom_id: str
    result: GenerationResult | None
    error: ProviderError | None = None


class BatchGateway(Protocol):
    """Optional batch processing (07 `submit_batch/get_batch`, M6 T24): cheaper and asynchronous. The batch
    ID is persisted before anything waits on it, so a restarted worker resumes the same batch."""

    async def submit_batch(self, items: list[tuple[str, GenerationRequest]]) -> str: ...

    async def batch_ended(self, batch_id: str) -> bool: ...

    async def batch_results(
        self, batch_id: str, requests: dict[str, GenerationRequest]
    ) -> list[BatchItem]: ...


def supports_batch(gateway: object) -> bool:
    return all(
        callable(getattr(gateway, name, None)) for name in ("submit_batch", "batch_ended", "batch_results")
    )


def provider_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Projection for strict structured output: drop keywords providers reject, keep structure closed."""

    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            out = {
                k: ({name: strip(sub) for name, sub in v.items()} if k == "properties" else strip(v))
                for k, v in node.items()
                if k not in UNSUPPORTED_SCHEMA_KEYWORDS
            }
            if out.get("type") == "object":
                out.setdefault("additionalProperties", False)
            if "minItems" in out and out["minItems"] not in (0, 1):
                del out["minItems"]
            return out
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    projected: dict[str, Any] = strip(schema)
    return projected
