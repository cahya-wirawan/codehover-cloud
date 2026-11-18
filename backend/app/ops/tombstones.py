"""`python -m app.ops.tombstones reapply`: replay deletions after a restore, before traffic (05, 22, A22).

A database restored from a backup taken before a deletion has lost that deletion. Its tombstone survives
in object storage (`tombstones/`, written when the deletion was requested). This command:
- marks every target deleted again, stops delivery and fences its tasks, exactly like the request did;
- resets each deletion request to pending, due now, so the purge sweeps again even if the restored
  database says it completed (restored object storage may hold copies the purge had removed).

It runs with the runtime role in each tombstone's own tenant context, like the API, so it needs no
privilege that bypasses row-level security. It is idempotent. Run it after migrations and before routing
any traffic to the restored environment; then let a worker purge.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.storage import ObjectStore
from app.core.tenancy import set_context
from app.engine.keys import TOMBSTONE_PREFIX
from app.jobs import store as jobs

logger = logging.getLogger("app.ops.tombstones")
SCHEMA = "urn:codehover-cloud:schema:tombstone:1.0.0"
SCOPES = ("project", "workspace", "account")


def _uuid(value: Any) -> UUID | None:
    return None if value is None else UUID(str(value))


async def _upsert_request(db: AsyncSession, doc: dict[str, Any]) -> None:
    """Recreate the request with its original ID, or reset it to pending and due now."""
    await db.execute(
        text(
            "INSERT INTO deletion_requests(id, scope, workspace_id, project_id, user_id, requested_at, not_before)"
            " VALUES (:id, :s, :w, :p, :u, CAST(:at AS timestamptz), now())"
            " ON CONFLICT (id) DO UPDATE SET state = 'pending', completed_at = NULL, lease_until = NULL,"
            " not_before = now(), error_code = NULL"
        ),
        {
            "id": _uuid(doc["id"]),
            "s": doc["scope"],
            "w": _uuid(doc.get("workspace_id")),
            "p": _uuid(doc.get("project_id")),
            "u": _uuid(doc.get("user_id")),
            "at": doc["requested_at"],
        },
    )


async def _project(db: AsyncSession, workspace_id: UUID, project_id: UUID) -> None:
    await db.execute(
        text(
            "UPDATE projects SET deleted_at = COALESCE(deleted_at, now()), updated_at = now()"
            " WHERE workspace_id = :w AND id = :p"
        ),
        {"w": workspace_id, "p": project_id},
    )
    await jobs.fence_project(db, workspace_id, project_id)


async def reapply_one(sessions: async_sessionmaker[AsyncSession], doc: dict[str, Any]) -> None:
    if doc.get("schema") != SCHEMA or doc.get("scope") not in SCOPES:
        raise ValueError("not a tombstone")
    workspace_id, project_id, user_id = (
        _uuid(doc.get("workspace_id")),
        _uuid(doc.get("project_id")),
        _uuid(doc.get("user_id")),
    )
    async with sessions() as db:
        if doc["scope"] == "project":
            assert workspace_id and project_id
            await set_context(db, workspace_id=workspace_id)
            await _project(db, workspace_id, project_id)
        elif doc["scope"] == "workspace":
            assert workspace_id
            await set_context(db, workspace_id=workspace_id)
            await db.execute(
                text(
                    "UPDATE workspaces SET status = 'deleting', deleted_at = COALESCE(deleted_at, now()),"
                    " updated_at = now() WHERE id = :w"
                ),
                {"w": workspace_id},
            )
            projects = (
                await db.execute(text("SELECT id FROM projects WHERE workspace_id = :w"), {"w": workspace_id})
            ).scalars()
            for pid in list(projects):
                await _project(db, workspace_id, pid)
        else:
            assert user_id
            await set_context(db, user_id=user_id)
            await db.execute(
                text(
                    "UPDATE users SET status = 'deleted', deleted_at = COALESCE(deleted_at, now()),"
                    " display_name = 'Deleted user', primary_email = NULL, updated_at = now() WHERE id = :u"
                ),
                {"u": user_id},
            )
            await db.execute(
                text("UPDATE sessions SET revoked_at = now() WHERE user_id = :u AND revoked_at IS NULL"),
                {"u": user_id},
            )
            await db.execute(text("DELETE FROM oauth_identities WHERE user_id = :u"), {"u": user_id})
        await _upsert_request(db, doc)
        await db.commit()


async def reapply(sessions: async_sessionmaker[AsyncSession], store: ObjectStore) -> dict[str, int]:
    """Reapply every tombstone, oldest first. Unreadable entries are counted and reported, never skipped
    silently: the operator must resolve them before routing traffic."""
    docs: list[dict[str, Any]] = []
    invalid = 0
    for key in await store.list(TOMBSTONE_PREFIX):
        try:
            docs.append(json.loads(await store.get(key)))
        except (ValueError, KeyError):
            invalid += 1
            logger.error("unreadable tombstone", extra={"key": key})
    applied = 0
    for doc in sorted(docs, key=lambda d: str(d.get("requested_at", ""))):
        try:
            await reapply_one(sessions, doc)
            applied += 1
        except (ValueError, KeyError, AssertionError):
            invalid += 1
            logger.error("invalid tombstone", extra={"tombstone_id": str(doc.get("id"))})
    return {"applied": applied, "invalid": invalid}


async def _main() -> int:
    from app.core.db import create_engine, session_factory
    from app.core.logging import configure_logging
    from app.core.settings import get_settings
    from app.main import object_store

    configure_logging()
    settings = get_settings()
    engine = create_engine(settings, application_name="tombstones")
    try:
        result = await reapply(session_factory(engine), object_store(settings))
    finally:
        await engine.dispose()
    logger.info("tombstones reapplied", extra=result)
    print(json.dumps(result))
    return 1 if result["invalid"] else 0


if __name__ == "__main__":
    if sys.argv[1:] != ["reapply"]:
        print("usage: python -m app.ops.tombstones reapply", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(_main()))
