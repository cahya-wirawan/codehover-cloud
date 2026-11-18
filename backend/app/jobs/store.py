"""Task state machine in SQL (04, 05 "Build states and consistency").

claim:    atomically lease a runnable task (pending, retry due, or lease expired); bumps the fencing
          `lease_generation` and the attempt count. Duplicate or stale notifications claim nothing.
complete: fenced by lease_generation: a worker that lost its lease cannot commit. The stage's database
          writes run inside the same transaction, after the fence check.
fail:     bounded retries (5/30/120 s + jitter, at most 3 retries) persisted as next_attempt_at;
          permanent errors or exhausted retries fail the build.
Every runnable task gets an outbox notification in the transaction that makes it runnable.

A task belongs to a build (pipeline stages) or to a snapshot (acquisition before any build, 0006).
A build that reaches a terminal state returns its unspent budget envelope in the same transaction.
"""

from __future__ import annotations

import json
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import set_context
from app.jobs.envelope import TaskEnvelope
from app.modules.usage import ledger

LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 20
MAX_ATTEMPTS = 4  # first try + three retries
BACKOFF_SECONDS = (5, 30, 120)
STAGES = ("acquire", "index", "explain", "render")  # M1 replay builds (started from a ref)
GENERATION_STAGES = (  # estimate builds
    "acquire",
    "index",
    "explain",
    "overview",
    "plan",
    "guide",
    "verify",
    "check",
    "render",
)
TERMINAL_CODES = {
    "budget_exhausted": ("budget_exhausted", "build.budget_exhausted"),
    "cancelled": ("cancelled", "build.cancelled"),
}


def pipeline(generation: bool) -> tuple[str, ...]:
    return GENERATION_STAGES if generation else STAGES


Apply = Callable[[AsyncSession], Awaitable[None]]


class LeaseLost(Exception):
    """The task was re-leased elsewhere (fencing): this worker's result must be discarded."""


@dataclass(frozen=True)
class Claim:
    task_id: UUID
    build_id: UUID | None
    workspace_id: UUID
    stage: str
    work_key: str
    generation: int
    attempt: int
    snapshot_id: UUID | None = None

    @property
    def build(self) -> UUID:
        """The owning build of a pipeline task (snapshot tasks have none)."""
        assert self.build_id is not None, "snapshot task has no build"
        return self.build_id


async def notify(
    db: AsyncSession,
    workspace_id: UUID,
    build_id: UUID | None,
    task_id: UUID,
    *,
    snapshot_id: UUID | None = None,
) -> None:
    envelope = TaskEnvelope(
        task_id=task_id, build_id=build_id, snapshot_id=snapshot_id, workspace_id=workspace_id
    )
    await db.execute(
        text(
            "INSERT INTO outbox(workspace_id, aggregate_id, event_type, payload)"
            " VALUES (:w, :t, 'task.ready', CAST(:p AS jsonb))"
        ),
        {"w": workspace_id, "t": task_id, "p": envelope.wire()},
    )
    await db.execute(
        text("UPDATE build_tasks SET notified_at = now() WHERE workspace_id = :w AND id = :t"),
        {"w": workspace_id, "t": task_id},
    )


async def event(
    db: AsyncSession,
    workspace_id: UUID,
    build_id: UUID,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append a build event; the build row lock serializes sequence numbers."""
    await db.execute(
        text("SELECT 1 FROM builds WHERE workspace_id = :w AND id = :b FOR UPDATE"),
        {"w": workspace_id, "b": build_id},
    )
    await db.execute(
        text(
            "INSERT INTO build_events(workspace_id, build_id, sequence, event_type, payload)"
            " SELECT :w, :b, COALESCE(MAX(sequence), 0) + 1, :t, CAST(:p AS jsonb)"
            " FROM build_events WHERE workspace_id = :w AND build_id = :b"
        ),
        {"w": workspace_id, "b": build_id, "t": event_type, "p": json.dumps(payload or {})},
    )


async def create_task(
    db: AsyncSession, workspace_id: UUID, build_id: UUID, stage: str, work_key: str
) -> UUID | None:
    task_id: UUID | None = (
        await db.execute(
            text(
                "INSERT INTO build_tasks(workspace_id, build_id, stage, work_key)"
                " VALUES (:w, :b, :s, :k) ON CONFLICT (build_id, stage, variant, work_key) DO NOTHING RETURNING id"
            ),
            {"w": workspace_id, "b": build_id, "s": stage, "k": work_key},
        )
    ).scalar_one_or_none()
    if task_id is not None:
        await notify(db, workspace_id, build_id, task_id)
    return task_id


async def create_snapshot_task(db: AsyncSession, workspace_id: UUID, snapshot_id: UUID) -> UUID | None:
    """Acquisition of a snapshot before any build; None if one is already pending or running."""
    task_id: UUID | None = (
        await db.execute(
            text(
                "INSERT INTO build_tasks(workspace_id, snapshot_id, stage, work_key)"
                " VALUES (:w, :s, 'acquire', 'snapshot:' || uuidv7()::text) ON CONFLICT DO NOTHING RETURNING id"
            ),
            {"w": workspace_id, "s": snapshot_id},
        )
    ).scalar_one_or_none()
    if task_id is not None:
        await notify(db, workspace_id, None, task_id, snapshot_id=snapshot_id)
    return task_id


_RUNNABLE = (
    "((t.state IN ('pending', 'retry_wait') AND (t.next_attempt_at IS NULL OR t.next_attempt_at <= now()))"
    " OR (t.state = 'leased' AND t.lease_until < now()))"
)
_LEASE = (
    "UPDATE build_tasks t SET state = 'leased', lease_generation = t.lease_generation + 1,"
    " attempt_count = t.attempt_count + 1, lease_until = now() + make_interval(secs => :lease),"
    " heartbeat_at = now(), updated_at = now()"
)


async def _claim_snapshot_task(db: AsyncSession, envelope: TaskEnvelope) -> Claim | None:
    row = (
        await db.execute(
            text(
                _LEASE
                + " WHERE t.id = :t AND t.workspace_id = :w AND t.snapshot_id = :s AND "
                + _RUNNABLE
                + " RETURNING t.id, t.stage, t.work_key, t.lease_generation, t.attempt_count"
            ),
            {
                "t": envelope.task_id,
                "w": envelope.workspace_id,
                "s": envelope.snapshot_id,
                "lease": LEASE_SECONDS,
            },
        )
    ).one_or_none()
    if row is None:
        await db.commit()
        return None
    await db.execute(
        text(
            "UPDATE snapshots SET status = 'acquiring', updated_at = now()"
            " WHERE workspace_id = :w AND id = :s AND status IN ('pending', 'failed')"
        ),
        {"w": envelope.workspace_id, "s": envelope.snapshot_id},
    )
    await db.commit()
    return Claim(
        row.id,
        None,
        envelope.workspace_id,
        row.stage,
        row.work_key,
        row.lease_generation,
        row.attempt_count,
        snapshot_id=envelope.snapshot_id,
    )


async def claim(db: AsyncSession, envelope: TaskEnvelope) -> Claim | None:
    await set_context(db, workspace_id=envelope.workspace_id)
    if envelope.snapshot_id is not None:
        return await _claim_snapshot_task(db, envelope)
    row = (
        await db.execute(
            text(
                "UPDATE build_tasks t SET state = 'leased', lease_generation = t.lease_generation + 1,"
                " attempt_count = t.attempt_count + 1, lease_until = now() + make_interval(secs => :lease),"
                " heartbeat_at = now(), updated_at = now()"
                " FROM builds b"
                " WHERE t.id = :t AND t.workspace_id = :w AND t.build_id = :b"
                "   AND b.workspace_id = t.workspace_id AND b.id = t.build_id"
                # Pipeline stages run on live builds; an export runs on a ready build (0011).
                "   AND ((t.stage <> 'export' AND b.status IN ('queued', 'running') AND b.cancel_requested_at IS NULL)"
                "        OR (t.stage = 'export' AND b.status = 'ready'))"
                "   AND ((t.state IN ('pending', 'retry_wait') AND (t.next_attempt_at IS NULL OR t.next_attempt_at <= now()))"
                "        OR (t.state = 'leased' AND t.lease_until < now()))"
                " RETURNING t.id, t.stage, t.work_key, t.lease_generation, t.attempt_count"
            ),
            {
                "t": envelope.task_id,
                "w": envelope.workspace_id,
                "b": envelope.build_id,
                "lease": LEASE_SECONDS,
            },
        )
    ).one_or_none()
    if row is None:
        await db.commit()
        return None
    await db.execute(
        text(
            "UPDATE builds SET status = 'running', updated_at = now() WHERE workspace_id = :w AND id = :b AND status = 'queued'"
        ),
        {"w": envelope.workspace_id, "b": envelope.build_id},
    )
    assert envelope.build_id is not None
    # Attempts started under an earlier lease never settled: their provider outcome is unknown, so they
    # keep their conservative hold until reconciled (07, 11; A09).
    await db.execute(
        text(
            "UPDATE provider_attempts SET state = 'unknown', error_code = COALESCE(error_code, 'lease_lost')"
            " WHERE workspace_id = :w AND task_id = :t AND state = 'started' AND lease_generation < :g"
        ),
        {"w": envelope.workspace_id, "t": row.id, "g": row.lease_generation},
    )
    if row.stage != "export":  # exports of a ready build do not belong to its progress stream
        await event(db, envelope.workspace_id, envelope.build_id, "stage.started", {"stage": row.stage})
    await db.commit()
    return Claim(
        row.id,
        envelope.build_id,
        envelope.workspace_id,
        row.stage,
        row.work_key,
        row.lease_generation,
        row.attempt_count,
    )


async def heartbeat(db: AsyncSession, claimed: Claim) -> bool:
    await set_context(db, workspace_id=claimed.workspace_id)
    alive = (
        await db.execute(
            text(
                "UPDATE build_tasks SET lease_until = now() + make_interval(secs => :lease), heartbeat_at = now()"
                " WHERE workspace_id = :w AND id = :t AND state = 'leased' AND lease_generation = :g RETURNING id"
            ),
            {
                "w": claimed.workspace_id,
                "t": claimed.task_id,
                "g": claimed.generation,
                "lease": LEASE_SECONDS,
            },
        )
    ).one_or_none()
    await db.commit()
    return alive is not None


async def _fence(
    db: AsyncSession,
    claimed: Claim,
    new_state: str,
    error_code: str | None = None,
    next_attempt_seconds: float | None = None,
) -> None:
    await set_context(db, workspace_id=claimed.workspace_id)
    updated = (
        await db.execute(
            text(
                "UPDATE build_tasks SET state = :s, lease_until = NULL, error_code = :e, updated_at = now(),"
                " next_attempt_at = CASE WHEN CAST(:n AS double precision) IS NULL THEN NULL"
                "   ELSE now() + make_interval(secs => CAST(:n AS double precision)) END"
                " WHERE workspace_id = :w AND id = :t AND state = 'leased' AND lease_generation = :g RETURNING id"
            ),
            {
                "s": new_state,
                "e": error_code,
                "n": next_attempt_seconds,
                "w": claimed.workspace_id,
                "t": claimed.task_id,
                "g": claimed.generation,
            },
        )
    ).one_or_none()
    if updated is None:
        await db.rollback()
        raise LeaseLost(str(claimed.task_id))


async def complete(db: AsyncSession, claimed: Claim, apply: Apply | None = None) -> None:
    """Fence, apply the stage's writes, enqueue the next stage (or finish the build), commit."""
    await _fence(db, claimed, "succeeded")
    if apply is not None:
        await apply(db)
    if claimed.build_id is None or claimed.stage == "export":  # the stage's apply recorded the outcome
        await db.commit()
        return
    await event(db, claimed.workspace_id, claimed.build_id, "stage.completed", {"stage": claimed.stage})
    generation = (
        await db.execute(
            text("SELECT config ? 'generation' FROM builds WHERE workspace_id = :w AND id = :b"),
            {"w": claimed.workspace_id, "b": claimed.build_id},
        )
    ).scalar_one()
    stages = pipeline(bool(generation))
    position = stages.index(claimed.stage)
    if await cancel_requested(db, claimed.workspace_id, claimed.build_id):
        await cancel_build(db, claimed.workspace_id, claimed.build_id)  # no further stage starts (A10)
    elif position + 1 < len(stages):
        await create_task(db, claimed.workspace_id, claimed.build_id, stages[position + 1], "build")
    else:
        await finish_build(db, claimed.workspace_id, claimed.build_id, "ready", "build.ready")
    await db.commit()


async def cancel_requested(db: AsyncSession, workspace_id: UUID, build_id: UUID) -> bool:
    requested = (
        await db.execute(
            text("SELECT cancel_requested_at IS NOT NULL FROM builds WHERE workspace_id = :w AND id = :b"),
            {"w": workspace_id, "b": build_id},
        )
    ).scalar_one()
    return bool(requested)


async def cancel_build(db: AsyncSession, workspace_id: UUID, build_id: UUID) -> bool:
    """Cancel the waiting tasks and finish the build: completed usage stays settled, the unspent envelope
    is released, holds for unknown outcomes remain (A10)."""
    await db.execute(
        text(
            "UPDATE build_tasks SET state = 'cancelled', next_attempt_at = NULL, updated_at = now()"
            " WHERE workspace_id = :w AND build_id = :b AND state IN ('pending', 'retry_wait')"
        ),
        {"w": workspace_id, "b": build_id},
    )
    return await finish_build(db, workspace_id, build_id, "cancelled", "build.cancelled")


async def finish_build(
    db: AsyncSession,
    workspace_id: UUID,
    build_id: UUID,
    status: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> bool:
    """Move a live build to a terminal state and release its unspent envelope (same transaction)."""
    finished = (
        await db.execute(
            text(
                "UPDATE builds SET status = :s, completed_at = now(), updated_at = now()"
                " WHERE workspace_id = :w AND id = :b AND status IN ('queued', 'running') RETURNING id"
            ),
            {"s": status, "w": workspace_id, "b": build_id},
        )
    ).one_or_none()
    if finished is None:
        return False
    await event(db, workspace_id, build_id, event_type, payload)
    released = await ledger.release_build(db, workspace_id, build_id)
    if released:
        await event(db, workspace_id, build_id, "usage.updated", {"released_micro_usd": released})
    return True


async def fail(
    db: AsyncSession, claimed: Claim, error_code: str, *, transient: bool, apply: Apply | None = None
) -> str:
    """Returns the resulting task state: retry_wait or failed."""
    retry = transient and claimed.attempt < MAX_ATTEMPTS and error_code not in TERMINAL_CODES
    delay = (
        BACKOFF_SECONDS[min(claimed.attempt, len(BACKOFF_SECONDS)) - 1] * (1 + random.random() * 0.2)  # noqa: S311 - jitter, not crypto
        if retry
        else None
    )
    await _fence(db, claimed, "retry_wait" if retry else "failed", error_code, delay)
    if claimed.build_id is None:  # snapshot acquisition
        if not retry:
            if apply is not None:
                await apply(db)
            await db.execute(
                text(
                    "UPDATE snapshots SET status = 'failed', rejection_code = COALESCE(rejection_code, :e),"
                    " updated_at = now() WHERE workspace_id = :w AND id = :s AND status IN ('pending', 'acquiring')"
                ),
                {"e": error_code, "w": claimed.workspace_id, "s": claimed.snapshot_id},
            )
        await db.commit()
        return "retry_wait" if retry else "failed"
    if claimed.stage == "export":  # a failed export never fails its (ready) build
        if not retry:
            await db.execute(
                text(
                    "UPDATE exports SET status = 'failed', error_code = :e, completed_at = now()"
                    " WHERE workspace_id = :w AND id = CAST(:x AS uuid) AND status = 'pending'"
                ),
                {"e": error_code, "w": claimed.workspace_id, "x": claimed.work_key},
            )
        await db.commit()
        return "retry_wait" if retry else "failed"
    if retry and await cancel_requested(db, claimed.workspace_id, claimed.build_id):
        await cancel_build(db, claimed.workspace_id, claimed.build_id)
    elif retry:
        await event(
            db,
            claimed.workspace_id,
            claimed.build_id,
            "task.retry_scheduled",
            {"stage": claimed.stage, "error": error_code},
        )
    else:
        if apply is not None:
            await apply(db)
        status, event_type = TERMINAL_CODES.get(error_code, ("failed", "build.failed"))
        await finish_build(
            db,
            claimed.workspace_id,
            claimed.build_id,
            status,
            event_type,
            {"stage": claimed.stage, "error": error_code},
        )
    await db.commit()
    return "retry_wait" if retry else "failed"
