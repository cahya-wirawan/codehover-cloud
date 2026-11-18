"""Task state machine in SQL (04, 05 "Build states and consistency").

claim:    atomically lease a runnable task (pending, retry due, or lease expired); bumps the fencing
          `lease_generation` and the attempt count. Duplicate or stale notifications claim nothing.
complete: fenced by lease_generation: a worker that lost its lease cannot commit. The stage's database
          writes run inside the same transaction, after the fence check.
fail:     bounded retries (5/30/120 s + jitter, at most 3 retries) persisted as next_attempt_at;
          permanent errors or exhausted retries fail the build.
Every runnable task gets an outbox notification in the transaction that makes it runnable.
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

LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 20
MAX_ATTEMPTS = 4  # first try + three retries
BACKOFF_SECONDS = (5, 30, 120)
STAGES = ("acquire", "index", "explain", "render")

Apply = Callable[[AsyncSession], Awaitable[None]]


class LeaseLost(Exception):
    """The task was re-leased elsewhere (fencing): this worker's result must be discarded."""


@dataclass(frozen=True)
class Claim:
    task_id: UUID
    build_id: UUID
    workspace_id: UUID
    stage: str
    work_key: str
    generation: int
    attempt: int


async def notify(db: AsyncSession, workspace_id: UUID, build_id: UUID, task_id: UUID) -> None:
    envelope = TaskEnvelope(task_id=task_id, build_id=build_id, workspace_id=workspace_id)
    await db.execute(
        text(
            "INSERT INTO outbox(workspace_id, aggregate_id, event_type, payload)"
            " VALUES (:w, :t, 'task.ready', CAST(:p AS jsonb))"
        ),
        {"w": workspace_id, "t": task_id, "p": envelope.model_dump_json()},
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


async def claim(db: AsyncSession, envelope: TaskEnvelope) -> Claim | None:
    await set_context(db, workspace_id=envelope.workspace_id)
    row = (
        await db.execute(
            text(
                "UPDATE build_tasks t SET state = 'leased', lease_generation = t.lease_generation + 1,"
                " attempt_count = t.attempt_count + 1, lease_until = now() + make_interval(secs => :lease),"
                " heartbeat_at = now(), updated_at = now()"
                " FROM builds b"
                " WHERE t.id = :t AND t.workspace_id = :w AND t.build_id = :b"
                "   AND b.workspace_id = t.workspace_id AND b.id = t.build_id"
                "   AND b.status IN ('queued', 'running') AND b.cancel_requested_at IS NULL"
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
    await event(db, claimed.workspace_id, claimed.build_id, "stage.completed", {"stage": claimed.stage})
    position = STAGES.index(claimed.stage)
    if position + 1 < len(STAGES):
        await create_task(db, claimed.workspace_id, claimed.build_id, STAGES[position + 1], "build")
    else:
        await db.execute(
            text(
                "UPDATE builds SET status = 'ready', completed_at = now(), updated_at = now()"
                " WHERE workspace_id = :w AND id = :b AND status = 'running'"
            ),
            {"w": claimed.workspace_id, "b": claimed.build_id},
        )
        await event(db, claimed.workspace_id, claimed.build_id, "build.ready")
    await db.commit()


async def fail(
    db: AsyncSession, claimed: Claim, error_code: str, *, transient: bool, apply: Apply | None = None
) -> str:
    """Returns the resulting task state: retry_wait or failed."""
    retry = transient and claimed.attempt < MAX_ATTEMPTS
    delay = (
        BACKOFF_SECONDS[min(claimed.attempt, len(BACKOFF_SECONDS)) - 1] * (1 + random.random() * 0.2)  # noqa: S311 - jitter, not crypto
        if retry
        else None
    )
    await _fence(db, claimed, "retry_wait" if retry else "failed", error_code, delay)
    if retry:
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
        await db.execute(
            text(
                "UPDATE builds SET status = 'failed', completed_at = now(), updated_at = now()"
                " WHERE workspace_id = :w AND id = :b AND status IN ('queued', 'running')"
            ),
            {"w": claimed.workspace_id, "b": claimed.build_id},
        )
        await event(
            db,
            claimed.workspace_id,
            claimed.build_id,
            "build.failed",
            {"stage": claimed.stage, "error": error_code},
        )
    await db.commit()
    return "retry_wait" if retry else "failed"
