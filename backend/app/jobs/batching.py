"""Batch prefill of a stage's provider requests (M6, T24; 07 "optional submit_batch/get_batch").

A build approved with `batch: true` sends its explain requests as one provider batch, priced with the
model's catalog batch price (a model without one cannot be batched: no silent price fallback). The flow
is a prefill of the tenant cache in front of the normal stage:

1. Earmark every uncached request at the batch price in one fenced transaction. The attempts are
   `batched`, so a re-claimed task never mistakes them for interrupted calls. The batch row is
   `submitting`.
2. Submit with no transaction open, then persist the provider's batch ID (`in_progress`) and wait:
   the task goes to `retry_wait` without using up an attempt (`StageWait`).
3. When the batch has ended, settle each answer in a fenced transaction: valid answers are charged at
   the batch price and written to the cache; invalid or errored ones are settled as failed (errored
   requests cost nothing). The batch is `completed`.
4. The stage then runs as usual: answers come from the cache, and anything the batch could not answer
   is called synchronously at the standard price, within the same build cap.

A worker that crashed between steps 1 and 2 cannot know whether the batch reached the provider, so the
batch is `abandoned` and its attempts become `unknown`, keeping their holds (07 "unknown outcomes keep
conservative holds"). A batch older than BATCH_MAX_AGE_SECONDS is abandoned the same way.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import set_context
from app.engine import generate
from app.engine.catalog import Price, cost_micro_usd
from app.engine.gateway import GenerationRequest, ProviderError, supports_batch
from app.jobs import calls
from app.jobs.store import Claim
from app.modules.usage import ledger

logger = logging.getLogger("app.jobs.batching")
POLL_SECONDS = 60
BATCH_MAX_AGE_SECONDS = 25 * 3600  # providers end batches within 24 hours
MAX_BATCH_REQUESTS = 10_000


class StageWait(Exception):
    """The stage waits for an external result: retry after `seconds` without using up an attempt."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"wait {seconds}s")
        self.seconds = seconds


Validate = Callable[[dict[str, Any]], list[str]]


def max_cost(request: GenerationRequest, price: Price) -> int:
    highest = max(price.input_per_mtok_micro_usd, price.cache_read_per_mtok_micro_usd or 0)
    bound = Price(input_per_mtok_micro_usd=highest, output_per_mtok_micro_usd=price.output_per_mtok_micro_usd)
    return cost_micro_usd(
        bound, input_tokens=request.input_token_bound(), output_tokens=request.max_output_tokens
    )


async def _current(db: AsyncSession, claimed: Claim) -> Any:
    await set_context(db, workspace_id=claimed.workspace_id)
    return (
        await db.execute(
            text(
                "SELECT id, state, provider_batch_id, items, created_at FROM provider_batches"
                " WHERE workspace_id = :w AND build_id = :b AND stage = :s ORDER BY created_at DESC LIMIT 1"
            ),
            {"w": claimed.workspace_id, "b": claimed.build, "s": claimed.stage},
        )
    ).one_or_none()


async def _abandon(db: AsyncSession, claimed: Claim, batch_id: Any, reason: str) -> None:
    """Outcome unknown: the attempts keep their holds as `unknown`; the stage continues synchronously."""
    await set_context(db, workspace_id=claimed.workspace_id)
    await db.execute(
        text(
            "UPDATE provider_attempts SET state = 'unknown', error_code = :e"
            " WHERE workspace_id = :w AND build_id = :b AND state = 'batched'"
            " AND id IN (SELECT CAST(i->>'custom_id' AS uuid) FROM provider_batches p,"
            "   jsonb_array_elements(p.items) i WHERE p.workspace_id = :w AND p.id = :id)"
        ),
        {"e": reason, "w": claimed.workspace_id, "b": claimed.build, "id": batch_id},
    )
    await db.execute(
        text(
            "UPDATE provider_batches SET state = 'abandoned', completed_at = now() WHERE workspace_id = :w AND id = :id"
        ),
        {"w": claimed.workspace_id, "id": batch_id},
    )
    await db.commit()
    logger.warning("provider batch abandoned", extra={"reason": reason})


async def _earmark_all(
    db: AsyncSession, claimed: Claim, items: list[tuple[generate.Prepared, Price]], provider: str
) -> Any:
    """Hold every request's maximum batch cost in the build envelope and record the batch, or None when the
    whole batch does not fit (the stage then runs synchronously and stops when the cap is reached)."""
    await set_context(db, workspace_id=claimed.workspace_id)
    build = await calls._lock_build(db, claimed)
    await calls._fence(db, claimed)
    if build.status != "running" or build.cancel_requested_at is not None:
        await db.rollback()
        raise calls.BuildStopped(build.status)
    maxima = [max_cost(p.request, price) for p, price in items]
    if (
        await ledger.unresolved_holds(db, claimed.workspace_id, claimed.build) + sum(maxima)
        > build.reserved_micro_usd
    ):
        await db.rollback()
        return None
    recorded = []
    for (prepared, price), maximum in zip(items, maxima, strict=True):
        request = prepared.request
        attempt = (
            await db.execute(
                text(
                    "INSERT INTO provider_attempts(workspace_id, build_id, task_id, attempt_number, lease_generation, role,"
                    " purpose, model_id, request_digest, price_snapshot, max_reserved_micro_usd, state)"
                    " SELECT :w, :b, :t, COALESCE(max(attempt_number), 0) + 1, :g, 'writer', :purpose, :model, :digest,"
                    " CAST(:price AS jsonb), :max, 'batched' FROM provider_attempts WHERE workspace_id = :w AND task_id = :t"
                    " RETURNING id"
                ),
                {
                    "w": claimed.workspace_id,
                    "b": claimed.build,
                    "t": claimed.task_id,
                    "g": claimed.generation,
                    "purpose": request.purpose,
                    "model": request.model.id,
                    "digest": request.digest(),
                    "price": json.dumps({"model": request.model.id, "batch": True, **price.model_dump()}),
                    "max": maximum,
                },
            )
        ).scalar_one()
        recorded.append({"custom_id": str(attempt), "cache_key": prepared.cache_key})
    batch = (
        await db.execute(
            text(
                "INSERT INTO provider_batches(workspace_id, build_id, task_id, stage, provider, items)"
                " VALUES (:w, :b, :t, :s, :p, CAST(:items AS jsonb)) RETURNING id, items"
            ),
            {
                "w": claimed.workspace_id,
                "b": claimed.build,
                "t": claimed.task_id,
                "s": claimed.stage,
                "p": provider,
                "items": json.dumps(recorded),
            },
        )
    ).one()
    await db.commit()
    return batch


async def _settle_results(
    db: AsyncSession,
    claimed: Claim,
    batch: Any,
    by_id: dict[str, tuple[generate.Prepared, Price, Validate]],
    results: dict[str, Any],
) -> None:
    await set_context(db, workspace_id=claimed.workspace_id)
    await calls._lock_build(db, claimed)
    await calls._fence(db, claimed)
    for custom_id, (prepared, price, validate) in by_id.items():
        item = results.get(custom_id)
        result = item.result if item is not None else None
        error: ProviderError | None = item.error if item is not None else None
        if item is None:  # the provider returned nothing for it: unknown, keep the hold
            state, cost, code = "unknown", None, "batch_missing"
        elif result is not None:
            problems = validate(result.output)
            usage = result.usage
            cost = cost_micro_usd(
                price,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
            )
            state, code = ("failed", "output_invalid") if problems else ("succeeded", "")
        else:
            usage = error.usage if error is not None else None
            cost = (
                cost_micro_usd(price, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)
                if usage
                else 0
            )
            state, code = "failed", error.code if error is not None else "batch_failed"
        await db.execute(
            text(
                "UPDATE provider_attempts SET state = :s, settled_micro_usd = :c, error_code = :e,"
                " input_tokens = :i, output_tokens = :o, output_digest = :d, completed_at = now()"
                " WHERE workspace_id = :w AND id = CAST(:a AS uuid) AND state = 'batched'"
            ),
            {
                "s": state,
                "c": cost if state != "unknown" else None,
                "e": code or None,
                "i": result.usage.input_tokens if result is not None else None,
                "o": result.usage.output_tokens if result is not None else None,
                "d": result.output_digest if result is not None else None,
                "w": claimed.workspace_id,
                "a": custom_id,
            },
        )
        if cost:
            await ledger.settle_attempt(db, claimed.workspace_id, claimed.build, UUID(custom_id), cost)
        if state == "succeeded" and result is not None:
            await db.execute(
                text(
                    "INSERT INTO generation_cache(workspace_id, cache_key, purpose, output, output_digest, model_id,"
                    " prompt_version, attempt_id) VALUES (:w, :k, :p, CAST(:o AS jsonb), :d, :m, :v, CAST(:a AS uuid))"
                    " ON CONFLICT (workspace_id, cache_key) DO NOTHING"
                ),
                {
                    "w": claimed.workspace_id,
                    "k": prepared.cache_key,
                    "p": prepared.request.purpose,
                    "o": json.dumps(result.output),
                    "d": result.output_digest,
                    "m": result.model_id,
                    "v": prepared.request.prompt_version,
                    "a": custom_id,
                },
            )
    await db.execute(
        text(
            "UPDATE provider_batches SET state = 'completed', completed_at = now() WHERE workspace_id = :w AND id = :id"
        ),
        {"w": claimed.workspace_id, "id": batch.id},
    )
    await db.commit()


async def prefill(
    sessions: Any, gateway: Any, claimed: Claim, work: list[tuple[generate.Prepared, Validate]]
) -> None:
    """Answer `work` through one provider batch, or return having done nothing the stage cannot redo."""
    if not work or not supports_batch(gateway):
        return
    async with sessions() as db:
        current = await _current(db, claimed)
        await db.commit()
    if current is not None and current.state in ("completed", "abandoned"):
        return  # this stage already had its batch; the rest runs synchronously
    by_cache = {p.cache_key: (p, v) for p, v in work}
    if current is None:
        pending: list[tuple[generate.Prepared, Price]] = []
        for prepared, _ in work:
            price = prepared.request.model.batch_price
            if price is None:
                return  # this model has no batch price: never batch it at a guessed price
            async with sessions() as db:
                if await calls.cached(db, claimed, prepared.cache_key) is None:
                    pending.append((prepared, price))
        if not pending:
            return
        pending = pending[:MAX_BATCH_REQUESTS]
        async with sessions() as db:
            batch = await _earmark_all(db, claimed, pending, pending[0][0].request.model.provider)
        if batch is None:
            return
        try:
            provider_id = await gateway.submit_batch(
                [(i["custom_id"], by_cache[i["cache_key"]][0].request) for i in batch.items]
            )
        except ProviderError:
            async with sessions() as db:
                await _abandon(db, claimed, batch.id, "batch_submit_failed")
            return
        async with sessions() as db:
            await set_context(db, workspace_id=claimed.workspace_id)
            await calls._fence(db, claimed)
            await db.execute(
                text(
                    "UPDATE provider_batches SET provider_batch_id = :p, state = 'in_progress'"
                    " WHERE workspace_id = :w AND id = :id"
                ),
                {"p": provider_id, "w": claimed.workspace_id, "id": batch.id},
            )
            await db.commit()
        raise StageWait(POLL_SECONDS)
    if current.state == "submitting":  # a worker stopped between recording and submitting
        async with sessions() as db:
            await _abandon(db, claimed, current.id, "batch_submission_unknown")
        return
    age = (datetime.now(UTC) - current.created_at).total_seconds()
    try:
        ended = await gateway.batch_ended(current.provider_batch_id)
    except ProviderError:
        ended = False
    if not ended:
        if age > BATCH_MAX_AGE_SECONDS:
            async with sessions() as db:
                await _abandon(db, claimed, current.id, "batch_expired")
            return
        raise StageWait(POLL_SECONDS)
    requests: dict[str, tuple[generate.Prepared, Price, Validate]] = {}
    for item in current.items:
        found = by_cache.get(item["cache_key"])
        if found is not None and found[0].request.model.batch_price is not None:
            requests[item["custom_id"]] = (found[0], found[0].request.model.batch_price, found[1])
    try:
        answers = await gateway.batch_results(
            current.provider_batch_id, {cid: p.request for cid, (p, _, _) in requests.items()}
        )
    except ProviderError as error:
        raise StageWait(POLL_SECONDS) from error
    async with sessions() as db:
        await _settle_results(db, claimed, current, requests, {a.custom_id: a for a in answers})
