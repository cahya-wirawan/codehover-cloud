"""Budgeted, fenced provider calls (07 "Budgets and latency", 11, 04).

One call is: earmark -> generate -> settle, each SQL step its own short transaction, never open during
the provider request.

- Earmark: lock the build, check the task still holds its lease (fencing) and the build is running and
  not cancelled, then require open earmarks + this call's maximum cost <= the build's reserved envelope.
  The maximum is the request's byte bound times the highest input price plus max output tokens times
  the output price, so the actual charge can never exceed it. A `started` attempt records it. When the
  maximum does not fit only because other calls of the build are in flight, the call waits for them to
  settle (actual costs are usually far below the maxima) instead of ending the build; it ends the build
  only when it cannot fit even with nothing in flight, or after HOLD_WAIT_SECONDS.
- Settle: in one fenced transaction, record usage and actual cost on the attempt, move the cost from
  reserved to settled (append-only ledger entry keyed by attempt), and write the validated answer to the
  tenant cache, which is the checkpoint a retried task resumes from. A worker that lost its lease writes
  nothing; its attempt stays `started` and becomes `unknown` when the task is claimed again.
- Outcomes: transient errors retry at most three times with backoff; an invalid answer gets one repair
  call; every retry and repair is its own budgeted attempt. An unknown outcome keeps its hold and is not
  retried here: the provider may have billed it, and retrying at once would add a worst-case hold per
  try. The call fails (`CallFailed("unknown")`), the stage stops, and the task retries later from its
  checkpoints.
  A call cancelled while its request is out (a worker shutting down) may still be billed: its attempt
  becomes `unknown` and keeps its hold. Unknown holds settle at their maximum after a day
  (`settle_stale_holds`).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.tenancy import set_context
from app.engine import generate
from app.engine.catalog import Price, cost_micro_usd
from app.engine.gateway import GenerationRequest, GenerationResult, ProviderError, Usage
from app.jobs.store import Claim, LeaseLost, event
from app.modules.usage import ledger

logger = logging.getLogger("app.jobs.calls")
MAX_TRANSIENT_RETRIES = 3
HOLD_POLL_SECONDS = 1.0  # how often a call waiting for room re-checks the envelope
HOLD_WAIT_SECONDS = 600.0  # longest wait for in-flight calls to settle (each is bounded at 90 s, 04)
_SETTLING: set[asyncio.Future[None]] = set()  # settlements of cut-off calls, kept until they finish


class BudgetExhausted(Exception):
    """No room is left in the build envelope for this call."""


class EnvelopeBusy(BudgetExhausted):
    """No room now, but enough once the build's in-flight calls settle: wait and earmark again."""


class BuildStopped(Exception):
    """The build was cancelled or is no longer running: start no new call (A10)."""


class CallFailed(Exception):
    def __init__(self, kind: str, code: str) -> None:
        super().__init__(code)
        self.kind = kind
        self.code = code


@dataclass(frozen=True)
class Outcome:
    output: dict[str, Any]
    cached: bool
    model_id: str
    attempts: int


def max_cost(request: GenerationRequest) -> int:
    price = request.model.price
    highest_input = max(
        price.input_per_mtok_micro_usd,
        price.cache_read_per_mtok_micro_usd or 0,
        price.cache_write_per_mtok_micro_usd or 0,
    )
    bound = Price(
        input_per_mtok_micro_usd=highest_input, output_per_mtok_micro_usd=price.output_per_mtok_micro_usd
    )
    return cost_micro_usd(
        bound, input_tokens=request.input_token_bound(), output_tokens=request.max_output_tokens
    )


def actual_cost(request: GenerationRequest, usage: Usage) -> int:
    return cost_micro_usd(
        request.model.price,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
    )


async def _fence(db: AsyncSession, claimed: Claim) -> None:
    held = (
        await db.execute(
            text(
                "SELECT 1 FROM build_tasks WHERE workspace_id = :w AND id = :t AND state = 'leased'"
                " AND lease_generation = :g FOR UPDATE"
            ),
            {"w": claimed.workspace_id, "t": claimed.task_id, "g": claimed.generation},
        )
    ).one_or_none()
    if held is None:
        await db.rollback()
        raise LeaseLost(str(claimed.task_id))


async def _lock_build(db: AsyncSession, claimed: Claim) -> Any:
    return (
        await db.execute(
            text(
                "SELECT status, cancel_requested_at, reserved_micro_usd FROM builds"
                " WHERE workspace_id = :w AND id = :b FOR UPDATE"
            ),
            {"w": claimed.workspace_id, "b": claimed.build},
        )
    ).one()


async def cached(db: AsyncSession, claimed: Claim, cache_key: str) -> dict[str, Any] | None:
    await set_context(db, workspace_id=claimed.workspace_id)
    row = (
        await db.execute(
            text(
                "SELECT output, model_id FROM generation_cache WHERE workspace_id = :w AND cache_key = :k"
                " AND expires_at > now()"
            ),
            {"w": claimed.workspace_id, "k": cache_key},
        )
    ).one_or_none()
    await db.commit()
    return None if row is None else {"output": row.output, "model_id": row.model_id}


async def _earmark(db: AsyncSession, claimed: Claim, request: GenerationRequest, maximum: int) -> Any:
    await set_context(db, workspace_id=claimed.workspace_id)
    build = await _lock_build(db, claimed)
    await _fence(db, claimed)
    if build.status != "running" or build.cancel_requested_at is not None:
        await db.rollback()
        raise BuildStopped(build.status)
    open_holds = await ledger.unresolved_holds(db, claimed.workspace_id, claimed.build)
    if open_holds + maximum > build.reserved_micro_usd:
        in_flight = await ledger.holds_in_flight(db, claimed.workspace_id, claimed.build)
        await db.rollback()
        left = build.reserved_micro_usd - open_holds
        if in_flight and open_holds - in_flight + maximum <= build.reserved_micro_usd:
            raise EnvelopeBusy(f"needs {maximum}, {left} left until {in_flight} in flight settles")
        raise BudgetExhausted(f"needs {maximum}, {left} left")
    attempt = (
        await db.execute(
            text(
                "INSERT INTO provider_attempts(workspace_id, build_id, task_id, attempt_number, lease_generation, role,"
                " purpose, model_id, request_digest, price_snapshot, max_reserved_micro_usd)"
                " SELECT :w, :b, :t, COALESCE(max(attempt_number), 0) + 1, :g, :role, :purpose, :model, :digest,"
                " CAST(:price AS jsonb), :max FROM provider_attempts WHERE workspace_id = :w AND task_id = :t"
                " RETURNING id, attempt_number"
            ),
            {
                "w": claimed.workspace_id,
                "b": claimed.build,
                "t": claimed.task_id,
                "g": claimed.generation,
                "role": "checker" if request.purpose == "check" else "writer",
                "purpose": request.purpose,
                "model": request.model.id,
                "digest": request.digest(),
                "price": json.dumps({"model": request.model.id, **request.model.price.model_dump()}),
                "max": maximum,
            },
        )
    ).one()
    await db.commit()
    return attempt


async def _settle(
    db: AsyncSession,
    claimed: Claim,
    request: GenerationRequest,
    attempt_id: Any,
    *,
    state: str,
    usage: Usage | None,
    provider_request_id: str | None,
    error_code: str | None,
    result: GenerationResult | None = None,
    cache_key: str | None = None,
) -> int:
    await set_context(db, workspace_id=claimed.workspace_id)
    await _lock_build(db, claimed)  # lock order everywhere: build row, then task row (no deadlocks)
    await _fence(db, claimed)
    cost = actual_cost(request, usage) if usage is not None and state != "unknown" else None
    await db.execute(
        text(
            "UPDATE provider_attempts SET state = :s, input_tokens = :i, output_tokens = :o, cache_read_tokens = :cr,"
            " cache_write_tokens = :cw, settled_micro_usd = :c, provider_request_id = :rid, error_code = :e,"
            " output_digest = :d, completed_at = now() WHERE workspace_id = :w AND id = :a AND state = 'started'"
        ),
        {
            "s": state,
            "i": usage.input_tokens if usage else None,
            "o": usage.output_tokens if usage else None,
            "cr": usage.cache_read_tokens if usage else None,
            "cw": usage.cache_write_tokens if usage else None,
            "c": (cost or 0) if state in ("succeeded", "failed") else None,
            "rid": provider_request_id,
            "e": error_code,
            "d": result.output_digest if result else None,
            "w": claimed.workspace_id,
            "a": attempt_id,
        },
    )
    charged = 0
    if cost:
        charged = await ledger.settle_attempt(db, claimed.workspace_id, claimed.build, attempt_id, cost)
    if result is not None and cache_key is not None:
        await db.execute(
            text(
                "INSERT INTO generation_cache(workspace_id, cache_key, purpose, output, output_digest, model_id,"
                " prompt_version, attempt_id) VALUES (:w, :k, :p, CAST(:o AS jsonb), :d, :m, :v, :a)"
                " ON CONFLICT (workspace_id, cache_key) DO UPDATE SET output = EXCLUDED.output,"
                " output_digest = EXCLUDED.output_digest, model_id = EXCLUDED.model_id, attempt_id = EXCLUDED.attempt_id,"
                " created_at = now(), expires_at = now() + interval '30 days'"
            ),
            {
                "w": claimed.workspace_id,
                "k": cache_key,
                "p": request.purpose
                if request.purpose != "repair"
                else request.evidence.get("repair_of", "repair"),
                "o": json.dumps(result.output),
                "d": result.output_digest,
                "m": result.model_id,
                "v": request.prompt_version,
                "a": attempt_id,
            },
        )
    if charged:
        await event(db, claimed.workspace_id, claimed.build, "usage.updated", {"settled_micro_usd": charged})
    await db.commit()
    provider = metrics.label(request.model.provider)
    metrics.PROVIDER_CALLS.labels(provider, metrics.label(request.purpose), state).inc()
    if charged:
        metrics.PROVIDER_COST.labels(provider).inc(charged)
    if usage is not None:
        metrics.PROVIDER_TOKENS.labels(provider, "input").inc(usage.input_tokens)
        metrics.PROVIDER_TOKENS.labels(provider, "output").inc(usage.output_tokens)
    return charged


async def _earmark_when_room(sessions: Any, claimed: Claim, request: GenerationRequest, maximum: int) -> Any:
    waited = 0.0
    while True:
        try:
            async with sessions() as db:
                return await _earmark(db, claimed, request, maximum)
        except EnvelopeBusy as busy:
            if waited >= HOLD_WAIT_SECONDS:
                raise BudgetExhausted(str(busy)) from busy
        await asyncio.sleep(HOLD_POLL_SECONDS)
        waited += HOLD_POLL_SECONDS


async def _settle_cancelled(
    sessions: Any, claimed: Claim, request: GenerationRequest, attempt_id: Any
) -> None:
    try:
        async with sessions() as db:
            await _settle(
                db,
                claimed,
                request,
                attempt_id,
                state="unknown",
                usage=None,
                provider_request_id=None,
                error_code="call_cancelled",
            )
    except LeaseLost:
        pass  # the next claim marks the attempt unknown (lease_lost)


async def run(
    sessions: Any,
    gateway: Any,
    claimed: Claim,
    prepared: generate.Prepared,
    validate: Callable[[dict[str, Any]], list[str]],
    *,
    retry_delays: tuple[float, ...] = (2.0, 8.0, 30.0),
) -> Outcome:
    """Answer one prepared request: from the tenant cache, or through budgeted provider attempts."""
    async with sessions() as db:
        hit = await cached(db, claimed, prepared.cache_key)
    if hit is not None and not validate(hit["output"]):
        metrics.CACHE.labels("hit").inc()
        return Outcome(hit["output"], True, hit["model_id"], 0)
    metrics.CACHE.labels("miss").inc()

    request = prepared.request
    repaired = False
    transient = 0
    attempts = 0
    while True:
        maximum = max_cost(request)
        attempt = await _earmark_when_room(sessions, claimed, request, maximum)
        attempts += 1
        try:
            result = await gateway.generate(request)
        except asyncio.CancelledError:
            # The request may already have reached the provider and been billed: unknown, hold kept. A
            # task of its own, so a second cancellation (the stage unwinding) cannot cut this off too.
            settling = asyncio.ensure_future(_settle_cancelled(sessions, claimed, request, attempt.id))
            _SETTLING.add(settling)
            settling.add_done_callback(_SETTLING.discard)
            raise
        except ProviderError as error:
            async with sessions() as db:
                await _settle(
                    db,
                    claimed,
                    request,
                    attempt.id,
                    state="unknown" if error.kind == "unknown" else "failed",
                    usage=error.usage or (None if error.kind == "unknown" else Usage()),
                    provider_request_id=error.provider_request_id,
                    error_code=error.code,
                )
            if error.kind == "transient" and transient < MAX_TRANSIENT_RETRIES:
                delay = retry_delays[min(transient, len(retry_delays) - 1)]
                if error.retry_after_seconds is not None:
                    delay = min(max(delay, error.retry_after_seconds), 60.0)
                transient += 1
                await asyncio.sleep(delay)
                continue
            if error.kind == "invalid" and not repaired:
                repaired = True
                request = generate.repair_request(prepared, {"unparseable": error.code}, [error.code])
                continue
            raise CallFailed(error.kind, error.code) from error
        problems = validate(result.output)
        if problems and not repaired:
            async with sessions() as db:
                await _settle(
                    db,
                    claimed,
                    request,
                    attempt.id,
                    state="failed",
                    usage=result.usage,
                    provider_request_id=result.provider_request_id,
                    error_code="output_invalid",
                    result=result,
                )
            repaired = True
            request = generate.repair_request(prepared, result.output, problems)
            continue
        if problems:
            async with sessions() as db:
                await _settle(
                    db,
                    claimed,
                    request,
                    attempt.id,
                    state="failed",
                    usage=result.usage,
                    provider_request_id=result.provider_request_id,
                    error_code="output_invalid",
                    result=result,
                )
            raise CallFailed("invalid", "output_invalid")
        async with sessions() as db:
            await _settle(
                db,
                claimed,
                request,
                attempt.id,
                state="succeeded",
                usage=result.usage,
                provider_request_id=result.provider_request_id,
                error_code=None,
                result=result,
                cache_key=prepared.cache_key,
            )
        return Outcome(result.output, False, result.model_id, attempts)
