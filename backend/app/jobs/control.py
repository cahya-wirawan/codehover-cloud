"""Control loops run as app_system (05): outbox dispatch and lease/retry reconciliation."""

from __future__ import annotations

import json
from typing import Any

import aio_pika
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.envelope import ROUTING_KEY, TaskEnvelope

DISPATCH_BATCH = 50
RENOTIFY_AFTER_SECONDS = 60


async def dispatch_once(db: AsyncSession, exchange: Any) -> int:
    """Publish due outbox rows with confirms, then mark them. Duplicates are harmless (claims dedupe)."""
    rows = list(
        await db.execute(
            text(
                "SELECT id, payload, dispatch_attempts FROM outbox"
                " WHERE published_at IS NULL AND available_at <= now()"
                " ORDER BY available_at LIMIT :n FOR UPDATE SKIP LOCKED"
            ),
            {"n": DISPATCH_BATCH},
        )
    )
    published = 0
    for row in rows:
        try:
            await exchange.publish(
                aio_pika.Message(
                    json.dumps(row.payload).encode(),
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    message_id=str(row.id),
                ),
                routing_key=ROUTING_KEY,
                mandatory=True,
            )
        except Exception:
            await db.execute(
                text(
                    "UPDATE outbox SET dispatch_attempts = dispatch_attempts + 1,"
                    " available_at = now() + make_interval(secs => LEAST(300, 5 * power(2, dispatch_attempts)))"
                    " WHERE id = :id"
                ),
                {"id": row.id},
            )
            continue
        await db.execute(text("UPDATE outbox SET published_at = now() WHERE id = :id"), {"id": row.id})
        published += 1
    await db.commit()
    return published


async def reconcile_once(db: AsyncSession) -> dict[str, int]:
    """Expire dead leases, then (re)notify runnable tasks not notified recently (lost messages, due retries)."""
    expired = list(
        await db.execute(
            text(
                "UPDATE build_tasks SET state = 'retry_wait', lease_until = NULL, next_attempt_at = now(),"
                " notified_at = NULL, updated_at = now()"  # the dead worker consumed the last notification
                " WHERE state = 'leased' AND lease_until < now() RETURNING id"
            )
        )
    )
    runnable = list(
        await db.execute(
            text(
                "SELECT id, workspace_id, build_id, snapshot_id FROM build_tasks"
                " WHERE (state = 'pending' OR (state = 'retry_wait' AND next_attempt_at <= now()))"
                "   AND (notified_at IS NULL OR notified_at < now() - make_interval(secs => :after))"
                " ORDER BY next_attempt_at NULLS FIRST LIMIT 200 FOR UPDATE SKIP LOCKED"
            ),
            {"after": RENOTIFY_AFTER_SECONDS},
        )
    )
    for task in runnable:
        envelope = TaskEnvelope(
            task_id=task.id,
            build_id=task.build_id,
            snapshot_id=task.snapshot_id,
            workspace_id=task.workspace_id,
        )
        await db.execute(
            text(
                "INSERT INTO outbox(workspace_id, aggregate_id, event_type, payload)"
                " VALUES (:w, :t, 'task.ready', CAST(:p AS jsonb))"
            ),
            {"w": task.workspace_id, "t": task.id, "p": envelope.wire()},
        )
        await db.execute(text("UPDATE build_tasks SET notified_at = now() WHERE id = :t"), {"t": task.id})
    await db.commit()
    return {"expired": len(expired), "notified": len(runnable)}
