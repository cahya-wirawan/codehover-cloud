"""`python -m app.ops.verify_restore`: check a restored environment before it serves traffic (22 Recovery).

Run after the database restore, `python -m app.ops.tombstones reapply` and a purge. With the runtime
role, per workspace in its own tenant context, it checks that:
- every ready build whose content is kept has all its artifacts in object storage, with the SHA-256 the
  database recorded, and every source blob of its snapshot likewise (database and objects agree);
- every publication points to a ready, unswept build of its own project whose manifest exists, so no
  pointer is restored to missing artifacts;
- every deletion request is completed and its target has no content rows and no objects left (no source
  resurrected by the restore).

Exit 0 when everything holds; otherwise exit 1 with the problems (identifiers only).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.storage import ObjectStore
from app.core.tenancy import set_context
from app.engine.keys import project_prefix


async def _sha(store: ObjectStore, key: str) -> str | None:
    try:
        return hashlib.sha256(await store.get(key)).hexdigest()
    except Exception:
        return None


async def verify_workspace(
    sessions: async_sessionmaker[AsyncSession], store: ObjectStore, workspace_id: UUID
) -> tuple[dict[str, int], list[str]]:
    problems: list[str] = []
    counts = {"builds": 0, "artifacts": 0, "blobs": 0, "publications": 0, "deletions": 0}
    async with sessions() as db:
        await set_context(db, workspace_id=workspace_id)
        builds = (
            await db.execute(
                text(
                    "SELECT b.id, b.snapshot_id FROM builds b JOIN projects p ON p.workspace_id = b.workspace_id"
                    " AND p.id = b.project_id WHERE b.workspace_id = :w AND b.status = 'ready'"
                    " AND b.content_purged_at IS NULL AND p.deleted_at IS NULL"
                ),
                {"w": workspace_id},
            )
        ).all()
        artifacts = (
            await db.execute(
                text("SELECT build_id, object_key, sha256 FROM artifacts WHERE workspace_id = :w"),
                {"w": workspace_id},
            )
        ).all()
        blobs = (
            await db.execute(
                text(
                    "SELECT DISTINCT f.snapshot_id, f.blob_key, f.sha256 FROM source_files f"
                    " WHERE f.workspace_id = :w AND f.blob_key IS NOT NULL"
                ),
                {"w": workspace_id},
            )
        ).all()
        publications = (
            await db.execute(
                text(
                    "SELECT x.project_id, x.build_id, b.status, b.content_purged_at, b.project_id AS build_project,"
                    " b.manifest_key, p.deleted_at FROM publications x JOIN builds b ON b.workspace_id = x.workspace_id"
                    " AND b.id = x.build_id JOIN projects p ON p.workspace_id = x.workspace_id AND p.id = x.project_id"
                    " WHERE x.workspace_id = :w"
                ),
                {"w": workspace_id},
            )
        ).all()
        deletions = (
            await db.execute(
                text("SELECT id, scope, project_id, state FROM deletion_requests WHERE workspace_id = :w"),
                {"w": workspace_id},
            )
        ).all()
        content: dict[UUID, int] = {}
        for d in deletions:
            if d.scope == "project":
                content[d.id] = (
                    await db.execute(
                        text(
                            "SELECT (SELECT count(*) FROM source_files f JOIN snapshots s ON s.id = f.snapshot_id"
                            "         WHERE s.workspace_id = :w AND s.project_id = :p)"
                            " + (SELECT count(*) FROM symbols x JOIN builds b ON b.id = x.build_id"
                            "    WHERE b.workspace_id = :w AND b.project_id = :p)"
                            " + (SELECT count(*) FROM guide_pages x JOIN builds b ON b.id = x.build_id"
                            "    WHERE b.workspace_id = :w AND b.project_id = :p)"
                        ),
                        {"w": workspace_id, "p": d.project_id},
                    )
                ).scalar_one()
        await db.commit()

    kept = {b.id: b.snapshot_id for b in builds}
    counts["builds"] = len(kept)
    for a in artifacts:
        if a.build_id in kept:
            counts["artifacts"] += 1
            if await _sha(store, a.object_key) != a.sha256:
                problems.append(f"artifact missing or altered: build {a.build_id}")
    snapshots = set(kept.values())
    for blob in blobs:
        if blob.snapshot_id in snapshots:
            counts["blobs"] += 1
            if await _sha(store, blob.blob_key) != blob.sha256:
                problems.append(f"source blob missing or altered: snapshot {blob.snapshot_id}")
    for pub in publications:
        counts["publications"] += 1
        if pub.deleted_at is not None:
            continue  # served by nobody; a kept operator takedown (0016)
        if pub.status != "ready" or pub.content_purged_at is not None or pub.build_project != pub.project_id:
            problems.append(f"publication of project {pub.project_id} points to an unusable build")
        elif not pub.manifest_key or not await store.exists(pub.manifest_key):
            problems.append(f"publication of project {pub.project_id} points to a build without its manifest")
    for d in deletions:
        counts["deletions"] += 1
        if d.state != "completed":
            problems.append(f"deletion {d.id} not completed ({d.state})")
        if d.scope == "project":
            if content.get(d.id):
                problems.append(f"deleted project {d.project_id} still has content rows")
            if await store.list(project_prefix(workspace_id, d.project_id)):
                problems.append(f"deleted project {d.project_id} still has objects")
    return counts, problems


async def verify(sessions: async_sessionmaker[AsyncSession], store: ObjectStore) -> dict[str, Any]:
    started = time.monotonic()
    totals: dict[str, int] = {"workspaces": 0}
    problems: list[str] = []
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
            counts, found = await verify_workspace(sessions, store, workspace_id)
            totals["workspaces"] += 1
            for key, value in counts.items():
                totals[key] = totals.get(key, 0) + value
            problems.extend(f"workspace {workspace_id}: {p}" for p in found)
        after = page[-1]
    return {
        **totals,
        "problems": problems,
        "seconds": round(time.monotonic() - started, 2),
        "ok": not problems,
    }


async def _main() -> int:
    from app.core.db import create_engine, session_factory
    from app.core.settings import get_settings
    from app.main import object_store

    settings = get_settings()
    engine = create_engine(settings, application_name="verify-restore")
    try:
        report = await verify(session_factory(engine), object_store(settings))
    finally:
        await engine.dispose()
    print(json.dumps(report, indent=1))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
