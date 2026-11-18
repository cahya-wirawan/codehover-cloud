"""Purge deleted projects, workspaces and accounts (T17; 05 "deletion_requests", 08 "Lifecycle", A22).

A request becomes due at `not_before`, after every lease fenced by the deletion has expired. The purger
claims it through `app_private.claim_deletion` (identifiers and a lease only), then works in the tenant
context of the request like any other worker:

- objects: everything under the project (or workspace) prefix, swept before and after the database step,
  so an object stored late by a fenced worker is still removed;
- rows with content: source files, symbols, references, explanations, guide pages, claim checks, search
  documents, artifacts, events, exports, publications (except operator takedowns, which keep their address
  reserved) and generation cache entries;
- kept as minimal tombstones: the project, build, task, snapshot, estimate and provider-attempt rows the
  usage ledger and audit log refer to, with names scrubbed. Accounting stays reconcilable (11).

A failed purge records an error code and is retried when its lease expires. Every step is idempotent.
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
from app.engine.keys import project_prefix, workspace_prefix
from app.modules.deletion.service import Tombstone, record_tombstone

logger = logging.getLogger("app.purge")
PURGE_LEASE_SECONDS = 600

# Children before parents (all foreign keys are NO ACTION). `:b` = the project's builds, `:s` = snapshots.
_PROJECT_CONTENT = (
    "DELETE FROM search_documents WHERE workspace_id = :w AND build_id = ANY(:b)",
    "DELETE FROM claim_checks WHERE workspace_id = :w AND build_id = ANY(:b)",
    "DELETE FROM reference_edges WHERE workspace_id = :w AND build_id = ANY(:b)",
    "DELETE FROM symbols WHERE workspace_id = :w AND build_id = ANY(:b)",
    "DELETE FROM file_explanations WHERE workspace_id = :w AND build_id = ANY(:b)",
    "DELETE FROM guide_pages WHERE workspace_id = :w AND build_id = ANY(:b)",
    "DELETE FROM artifacts WHERE workspace_id = :w AND build_id = ANY(:b)",
    "DELETE FROM build_events WHERE workspace_id = :w AND build_id = ANY(:b)",
    "DELETE FROM exports WHERE workspace_id = :w AND build_id = ANY(:b)",
    # An operator takedown survives: its row (identifiers only) keeps the public address reserved.
    "DELETE FROM publications WHERE workspace_id = :w AND project_id = :p"
    " AND suspended_reason IS DISTINCT FROM 'operator'",
    "DELETE FROM generation_cache WHERE workspace_id = :w AND attempt_id IN"
    " (SELECT id FROM provider_attempts WHERE workspace_id = :w AND build_id = ANY(:b))",
    "DELETE FROM source_files WHERE workspace_id = :w AND snapshot_id = ANY(:s)",
    "UPDATE projects SET name = 'Deleted project', config = '{}'::jsonb, tracked_ref = '',"
    " last_observed_sha = NULL, last_observed_at = NULL, updated_at = now() WHERE workspace_id = :w AND id = :p",
)


async def _purge_project(
    sessions: async_sessionmaker[AsyncSession], store: ObjectStore, workspace_id: UUID, project_id: UUID
) -> dict[str, int]:
    prefix = project_prefix(workspace_id, project_id)
    swept = await store.delete_prefix(prefix)
    async with sessions() as db:
        await set_context(db, workspace_id=workspace_id)
        builds = list(
            (
                await db.execute(
                    text("SELECT id FROM builds WHERE workspace_id = :w AND project_id = :p"),
                    {"w": workspace_id, "p": project_id},
                )
            ).scalars()
        )
        snapshots = list(
            (
                await db.execute(
                    text("SELECT id FROM snapshots WHERE workspace_id = :w AND project_id = :p"),
                    {"w": workspace_id, "p": project_id},
                )
            ).scalars()
        )
        rows = 0
        for statement in _PROJECT_CONTENT:
            result: Any = await db.execute(
                text(statement), {"w": workspace_id, "p": project_id, "b": builds, "s": snapshots}
            )
            rows += result.rowcount or 0
        await db.commit()
    swept += await store.delete_prefix(prefix)  # anything a fenced worker stored meanwhile
    return {"objects": swept, "rows": rows}


async def _purge_workspace(
    sessions: async_sessionmaker[AsyncSession], store: ObjectStore, workspace_id: UUID
) -> dict[str, int]:
    async with sessions() as db:
        await set_context(db, workspace_id=workspace_id)
        projects = list(
            (
                await db.execute(text("SELECT id FROM projects WHERE workspace_id = :w"), {"w": workspace_id})
            ).scalars()
        )
    totals = {"objects": 0, "rows": 0}
    for project_id in projects:
        for key, value in (await _purge_project(sessions, store, workspace_id, project_id)).items():
            totals[key] += value
    async with sessions() as db:
        await set_context(db, workspace_id=workspace_id)
        for statement in (
            "DELETE FROM generation_cache WHERE workspace_id = :w",
            "DELETE FROM invitations WHERE workspace_id = :w",
            # The workspace is `deleting`, so the owner invariant no longer applies (0003).
            "DELETE FROM memberships WHERE workspace_id = :w",
            "UPDATE repository_connections SET status = 'revoked', updated_at = now()"
            " WHERE workspace_id = :w AND status <> 'revoked'",
            "UPDATE workspaces SET name = 'Deleted workspace', updated_at = now() WHERE id = :w",
        ):
            result: Any = await db.execute(text(statement), {"w": workspace_id})
            totals["rows"] += result.rowcount or 0
        await db.commit()
    totals["objects"] += await store.delete_prefix(workspace_prefix(workspace_id))
    return totals


async def _purge_account(sessions: async_sessionmaker[AsyncSession], user_id: UUID) -> dict[str, int]:
    async with sessions() as db:
        await set_context(db, user_id=user_id)
        result: Any = await db.execute(
            text("DELETE FROM idempotency_keys WHERE user_id = :u"), {"u": user_id}
        )
        await db.commit()
    return {"objects": 0, "rows": result.rowcount or 0}


async def _finish(sessions: async_sessionmaker[AsyncSession], claimed: Any, error_code: str | None) -> None:
    async with sessions() as db:
        await set_context(db, workspace_id=claimed.workspace_id, user_id=claimed.user_id)
        if error_code is None:
            await db.execute(
                text(
                    "UPDATE deletion_requests SET state = 'completed', completed_at = now(), lease_until = NULL,"
                    " error_code = NULL WHERE id = :id"
                ),
                {"id": claimed.id},
            )
        else:  # the lease stays: the request is claimed again once it expires
            await db.execute(
                text("UPDATE deletion_requests SET error_code = :e WHERE id = :id"),
                {"id": claimed.id, "e": error_code},
            )
        await db.commit()


async def purge_once(sessions: async_sessionmaker[AsyncSession], store: ObjectStore) -> dict[str, Any] | None:
    """Purge one due request; None when nothing is due."""
    async with sessions() as db:
        claimed = (
            await db.execute(
                text(
                    "SELECT id, scope, workspace_id, project_id, user_id, attempts, requested_at"
                    " FROM app_private.claim_deletion(:lease)"
                ),
                {"lease": PURGE_LEASE_SECONDS},
            )
        ).one_or_none()
        await db.commit()
    if claimed is None:
        return None
    log = {"deletion_id": str(claimed.id), "scope": claimed.scope, "attempt": claimed.attempts}
    tombstone = Tombstone(
        claimed.id,
        claimed.scope,
        claimed.workspace_id,
        claimed.project_id,
        claimed.user_id,
        claimed.requested_at,
        claimed.requested_at,
    )
    try:
        await record_tombstone(store, tombstone)  # repairs a copy the request could not store
        if claimed.scope == "project":
            counts = await _purge_project(sessions, store, claimed.workspace_id, claimed.project_id)
        elif claimed.scope == "workspace":
            counts = await _purge_workspace(sessions, store, claimed.workspace_id)
        else:
            counts = await _purge_account(sessions, claimed.user_id)
    except Exception as error:
        logger.exception("purge failed; retried after its lease", extra=log)
        await _finish(sessions, claimed, f"purge_{type(error).__name__.lower()}"[:64])
        metrics.PURGES.labels(claimed.scope, "failed").inc()
        return {**log, "outcome": "failed"}
    await _finish(sessions, claimed, None)
    metrics.PURGES.labels(claimed.scope, "completed").inc()
    logger.info("purged", extra={**log, **counts})
    return {**log, **counts, "outcome": "completed"}


async def purge_due(sessions: async_sessionmaker[AsyncSession], store: ObjectStore, limit: int = 20) -> int:
    done = 0
    while done < limit and await purge_once(sessions, store) is not None:
        done += 1
    return done
