"""Retention sweeps (T17; 05 "Retention and migrations", 08 "Lifecycle and controls", 10).

Run hourly by the worker's maintenance loop. Tenant data is swept one workspace at a time in that
workspace's context (app_runtime); identity and audit rows through `app_private.sweep_*`, whose deletes
row-level security bounds to rows past their window (0016). Every step is idempotent.

Per workspace:
- provider attempts whose outcome stayed unknown for a day (or that are still `started` a day later)
  settle at their maximum and become `reconciled`, so their holds stop counting against the allowance;
- ready exports past their 24 hours lose their object and become `expired` (downloads stay 410);
- build events after 30 days, generation cache entries when they expire, unused estimates after 30 days,
  owner notices 90 days after they were sent or given up;
- build content (rows and objects) of failed, cancelled or budget-exhausted builds after 7 days, and of
  ready builds that are not published and not among their project's 5 newest ready builds after 30 days;
  the build row stays, marked `content_purged_at`, for the ledger and history;
- snapshot source (file rows and blobs) of snapshots that failed or were rejected, after 7 days, and of
  older scans that no unswept build uses, after 30 days; each project keeps its newest scan.

The published build is never swept: the sweep locks the build and re-checks the publication pointer in
the same transaction, and publishing takes FOR SHARE on the build, so the two serialize.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.storage import ObjectStore
from app.core import metrics
from app.core.tenancy import set_context
from app.engine.keys import build_prefix, snapshot_prefix
from app.jobs.store import event
from app.modules.usage import ledger

logger = logging.getLogger("app.retention")

FAILED_BUILD_DAYS = 7
UNPUBLISHED_READY_DAYS = 30
KEEP_READY_BUILDS = 5
EVENT_DAYS = 30
ESTIMATE_DAYS = 30
FAILED_SNAPSHOT_DAYS = 7
NOTICE_DAYS = 90

_BUILD_CONTENT = (
    "DELETE FROM search_documents WHERE workspace_id = :w AND build_id = :b",
    "DELETE FROM claim_checks WHERE workspace_id = :w AND build_id = :b",
    "DELETE FROM reference_edges WHERE workspace_id = :w AND build_id = :b",
    "DELETE FROM symbols WHERE workspace_id = :w AND build_id = :b",
    "DELETE FROM file_explanations WHERE workspace_id = :w AND build_id = :b",
    "DELETE FROM guide_pages WHERE workspace_id = :w AND build_id = :b",
    "DELETE FROM artifacts WHERE workspace_id = :w AND build_id = :b",
    "UPDATE exports SET status = 'expired', object_key = NULL"
    " WHERE workspace_id = :w AND build_id = :b AND status = 'ready'",
)

_DUE_BUILDS = text(
    "SELECT b.id, b.project_id FROM builds b"
    " WHERE b.workspace_id = :w AND b.content_purged_at IS NULL AND b.completed_at IS NOT NULL"
    "   AND NOT EXISTS (SELECT 1 FROM publications p WHERE p.workspace_id = b.workspace_id AND p.build_id = b.id)"
    "   AND ((b.status IN ('failed', 'cancelled', 'budget_exhausted')"
    "         AND b.completed_at < now() - make_interval(days => :failed_days))"
    "     OR (b.status = 'ready' AND b.completed_at < now() - make_interval(days => :ready_days)"
    "         AND b.id NOT IN (SELECT r.id FROM builds r WHERE r.workspace_id = b.workspace_id"
    "                          AND r.project_id = b.project_id AND r.status = 'ready'"
    "                          ORDER BY r.completed_at DESC, r.id DESC LIMIT :keep)))"
    " ORDER BY b.completed_at LIMIT 50"
)


async def _purge_build(
    sessions: async_sessionmaker[AsyncSession], store: ObjectStore, workspace_id: UUID, build: Any
) -> bool:
    async with sessions() as db:
        await set_context(db, workspace_id=workspace_id)
        locked = (
            await db.execute(
                text(
                    "SELECT id FROM builds WHERE workspace_id = :w AND id = :b AND content_purged_at IS NULL"
                    " FOR UPDATE"
                ),
                {"w": workspace_id, "b": build.id},
            )
        ).one_or_none()
        # A separate statement after the lock: under READ COMMITTED it takes a fresh snapshot, so it sees a
        # publication committed while this sweep waited for publish's FOR SHARE on the build. (A NOT EXISTS
        # inside the locking statement would keep the old snapshot; security review of 0.5.15.)
        published = (
            locked is not None
            and (
                await db.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM publications WHERE workspace_id = :w AND build_id = :b)"
                    ),
                    {"w": workspace_id, "b": build.id},
                )
            ).scalar_one()
        )
        if locked is None or published:  # already swept, or published meanwhile
            await db.commit()
            return False
        for statement in _BUILD_CONTENT:
            await db.execute(text(statement), {"w": workspace_id, "b": build.id})
        await db.execute(
            text("UPDATE builds SET content_purged_at = now() WHERE workspace_id = :w AND id = :b"),
            {"w": workspace_id, "b": build.id},
        )
        await db.commit()
    # After the commit: no reader can reach these objects any more (their rows are gone).
    await store.delete_prefix(build_prefix(workspace_id, build.project_id, build.id))
    return True


async def _settle_stale_holds(sessions: async_sessionmaker[AsyncSession], workspace_id: UUID) -> int:
    async with sessions() as db:
        await set_context(db, workspace_id=workspace_id)
        stale = (
            await db.execute(
                text(
                    "SELECT id, build_id FROM provider_attempts WHERE workspace_id = :w"
                    " AND state IN ('started', 'unknown') AND created_at < now() - make_interval(hours => :h)"
                    " ORDER BY created_at LIMIT 200"
                ),
                {"w": workspace_id, "h": ledger.STALE_HOLD_HOURS},
            )
        ).all()
        await db.commit()
    settled = 0
    for attempt in stale:  # one short transaction per attempt
        async with sessions() as db:
            await set_context(db, workspace_id=workspace_id)
            charged = await ledger.settle_stale_hold(db, workspace_id, attempt.build_id, attempt.id)
            if charged:
                await event(
                    db, workspace_id, attempt.build_id, "usage.updated", {"settled_micro_usd": charged}
                )
            await db.commit()
        settled += 1 if charged else 0
    return settled


async def sweep_workspace(
    sessions: async_sessionmaker[AsyncSession], store: ObjectStore, workspace_id: UUID
) -> dict[str, int]:
    counts = {
        "exports": 0,
        "events": 0,
        "cache": 0,
        "estimates": 0,
        "notices": 0,
        "builds": 0,
        "snapshots": 0,
    }
    counts["stale_holds"] = await _settle_stale_holds(sessions, workspace_id)
    async with sessions() as db:
        await set_context(db, workspace_id=workspace_id)
        expired = (
            await db.execute(
                text(
                    "SELECT id, object_key FROM exports WHERE workspace_id = :w AND status = 'ready'"
                    " AND expires_at < now() ORDER BY expires_at LIMIT 200"
                ),
                {"w": workspace_id},
            )
        ).all()
        await db.commit()
    for export in expired:
        if export.object_key:
            await store.delete(export.object_key)
        async with sessions() as db:
            await set_context(db, workspace_id=workspace_id)
            await db.execute(
                text(
                    "UPDATE exports SET status = 'expired', object_key = NULL"
                    " WHERE workspace_id = :w AND id = :e AND status = 'ready'"
                ),
                {"w": workspace_id, "e": export.id},
            )
            await db.commit()
        counts["exports"] += 1

    async with sessions() as db:
        await set_context(db, workspace_id=workspace_id)
        for key, statement in (
            (
                "events",
                "DELETE FROM build_events WHERE workspace_id = :w AND created_at < now() - make_interval(days => :ev)",
            ),
            ("cache", "DELETE FROM generation_cache WHERE workspace_id = :w AND expires_at < now()"),
            (
                "estimates",
                "DELETE FROM estimates e WHERE e.workspace_id = :w AND e.created_at < now() - make_interval(days => :es)"
                " AND NOT EXISTS (SELECT 1 FROM builds b WHERE b.workspace_id = e.workspace_id AND b.estimate_id = e.id)",
            ),
            (
                "notices",
                "DELETE FROM owner_notices WHERE workspace_id = :w AND created_at < now() - make_interval(days => :no)"
                " AND state NOT IN ('pending', 'sending')",
            ),
        ):
            result: Any = await db.execute(
                text(statement),
                {"w": workspace_id, "ev": EVENT_DAYS, "es": ESTIMATE_DAYS, "no": NOTICE_DAYS},
            )
            counts[key] += result.rowcount or 0
        due = (
            await db.execute(
                _DUE_BUILDS,
                {
                    "w": workspace_id,
                    "failed_days": FAILED_BUILD_DAYS,
                    "ready_days": UNPUBLISHED_READY_DAYS,
                    "keep": KEEP_READY_BUILDS,
                },
            )
        ).all()
        snapshots = (
            await db.execute(
                text(
                    "SELECT s.id, s.project_id FROM snapshots s WHERE s.workspace_id = :w"
                    " AND s.content_purged_at IS NULL"
                    " AND ((s.status IN ('failed', 'rejected') AND s.updated_at < now() - make_interval(days => :d))"
                    # Source no live build needs: older than 30 days and not the project's newest scan.
                    "   OR (s.status = 'scanned' AND s.updated_at < now() - make_interval(days => :old)"
                    "       AND s.id <> (SELECT n.id FROM snapshots n WHERE n.workspace_id = s.workspace_id"
                    "                    AND n.project_id = s.project_id AND n.status = 'scanned'"
                    "                    ORDER BY n.created_at DESC, n.id DESC LIMIT 1)))"
                    " AND NOT EXISTS (SELECT 1 FROM builds b WHERE b.workspace_id = s.workspace_id"
                    "                 AND b.snapshot_id = s.id AND b.content_purged_at IS NULL)"
                    " LIMIT 50"
                ),
                {"w": workspace_id, "d": FAILED_SNAPSHOT_DAYS, "old": UNPUBLISHED_READY_DAYS},
            )
        ).all()
        await db.commit()
    for build in due:
        counts["builds"] += await _purge_build(sessions, store, workspace_id, build)
    for snapshot in snapshots:
        async with sessions() as db:
            await set_context(db, workspace_id=workspace_id)
            await db.execute(
                text("DELETE FROM source_files WHERE workspace_id = :w AND snapshot_id = :s"),
                {"w": workspace_id, "s": snapshot.id},
            )
            await db.execute(
                text("UPDATE snapshots SET content_purged_at = now() WHERE workspace_id = :w AND id = :s"),
                {"w": workspace_id, "s": snapshot.id},
            )
            await db.commit()
        await store.delete_prefix(snapshot_prefix(workspace_id, snapshot.project_id, snapshot.id))
        counts["snapshots"] += 1
    return counts


async def sweep(sessions: async_sessionmaker[AsyncSession], store: ObjectStore) -> dict[str, int]:
    """One full pass: every workspace, then identity rows and old audit events."""
    totals: dict[str, int] = {}
    after: UUID | None = None
    while True:
        async with sessions() as db:
            page = list(
                (
                    await db.execute(text("SELECT app_private.workspaces_after(:a, 100)"), {"a": after})
                ).scalars()
            )
            await db.commit()
        if not page:
            break
        for workspace_id in page:
            try:
                for key, value in (await sweep_workspace(sessions, store, workspace_id)).items():
                    totals[key] = totals.get(key, 0) + value
            except Exception:
                logger.exception("retention sweep failed", extra={"workspace_id": str(workspace_id)})
        after = page[-1]
    async with sessions() as db:
        identity = (await db.execute(text("SELECT * FROM app_private.sweep_identity()"))).one()
        audit = (await db.execute(text("SELECT app_private.sweep_audit()"))).scalar_one()
        oauth = (await db.execute(text("SELECT app_private.sweep_oauth()"))).scalar_one()
        handles = (await db.execute(text("SELECT app_private.sweep_handle_reservations()"))).scalar_one()
        handles += (await db.execute(text("SELECT app_private.sweep_project_slug_redirects()"))).scalar_one()
        windows: Any = await db.execute(  # rate-limit counters (0019): no tenant data, kept two days
            text("DELETE FROM rate_limits WHERE window_start < now() - interval '2 days'")
        )
        await db.commit()
    for key, value in totals.items():
        metrics.RETENTION.labels(kind=key).inc(value)
    totals.update(
        {
            "idempotency_keys": identity.idempotency_keys,
            "oauth_transactions": identity.oauth_transactions,
            "sessions": identity.sessions,
            "audit_events": audit,
            "oauth": oauth,
            "handle_reservations": handles,
            "rate_limit_windows": int(windows.rowcount or 0),
        }
    )
    for key in (
        "idempotency_keys",
        "oauth_transactions",
        "sessions",
        "audit_events",
        "oauth",
        "handle_reservations",
        "rate_limit_windows",
    ):
        metrics.RETENTION.labels(kind=key).inc(totals[key])
    return totals
