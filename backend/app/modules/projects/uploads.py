"""ZIP repository import (M6; 01 P1, 08 "Archive traversal").

A project can come from an uploaded ZIP instead of GitHub. Each upload is one immutable snapshot whose
commit identifier is the archive's SHA-256, so uploading the same archive twice reuses its snapshot.
The archive is stored under the snapshot's object prefix and acquired like a GitHub tarball, with the
same limits (100 MiB compressed, 250 MiB expanded, 10,000 entries, depth 30) applied by
`acquire.extract_zip` in the worker; the request only checks the size and the ZIP signature.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from sqlalchemy import exc, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage import ObjectStore
from app.core.errors import ApiError
from app.core.settings import Settings
from app.core.tenancy import set_context
from app.engine.acquire import MAX_COMPRESSED_BYTES
from app.engine.keys import upload_key
from app.jobs import store as jobs
from app.modules.projects import estimates
from app.modules.workspaces.access import Member
from app.modules.workspaces.service import audit

UPLOAD_REF = "upload"
ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06")  # a local file header, or an empty archive


async def _upload_connection(db: AsyncSession, member: Member) -> UUID:
    """The workspace's single upload connection, created on first use."""
    await db.execute(
        text(
            "INSERT INTO repository_connections(workspace_id, provider, connected_by)"
            " VALUES (:w, 'upload', :u) ON CONFLICT (workspace_id) WHERE provider = 'upload' DO NOTHING"
        ),
        {"w": member.workspace_id, "u": member.user_id},
    )
    connection: UUID = (
        await db.execute(
            text("SELECT id FROM repository_connections WHERE workspace_id = :w AND provider = 'upload'"),
            {"w": member.workspace_id},
        )
    ).scalar_one()
    return connection


async def create_upload_project(db: AsyncSession, member: Member, name: str, slug: str) -> UUID:
    connection = await _upload_connection(db, member)
    repository: UUID = (
        await db.execute(
            text(
                "INSERT INTO repositories(workspace_id, connection_id, owner, name, visibility, default_branch)"
                " VALUES (:w, :c, 'upload', :n, 'private', :ref) RETURNING id"
            ),
            {"w": member.workspace_id, "c": connection, "n": slug, "ref": UPLOAD_REF},
        )
    ).scalar_one()
    try:
        project_id: UUID = (
            await db.execute(
                text(
                    "INSERT INTO projects(workspace_id, repository_id, name, slug, tracked_ref)"
                    " VALUES (:w, :r, :n, :s, :ref) RETURNING id"
                ),
                {"w": member.workspace_id, "r": repository, "n": name, "s": slug, "ref": UPLOAD_REF},
            )
        ).scalar_one()
    except exc.IntegrityError as error:
        await db.rollback()
        raise ApiError(409, "SLUG_TAKEN", "A project with this address already exists.") from error
    await audit(db, member, "project.created", "project", project_id, {"source": "upload"})
    return project_id


async def is_upload(db: AsyncSession, workspace_id: UUID, project_id: UUID) -> bool:
    provider = (
        await db.execute(
            text(
                "SELECT c.provider FROM projects p"
                " JOIN repositories r ON r.workspace_id = p.workspace_id AND r.id = p.repository_id"
                " JOIN repository_connections c ON c.workspace_id = r.workspace_id AND c.id = r.connection_id"
                " WHERE p.workspace_id = :w AND p.id = :p AND p.deleted_at IS NULL"
            ),
            {"w": workspace_id, "p": project_id},
        )
    ).scalar_one_or_none()
    if provider is None:
        raise ApiError(404, "NOT_FOUND", "Project not found.")
    return bool(provider == "upload")


def check_archive(data: bytes) -> str:
    """The archive's SHA-256 (the snapshot's commit identifier) after the cheap request-time checks."""
    if not data:
        raise ApiError(422, "UPLOAD_EMPTY", "The upload is empty.")
    if len(data) > MAX_COMPRESSED_BYTES:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", "Archives are limited to 100 MiB.")
    if not data.startswith(ZIP_SIGNATURES):
        raise ApiError(422, "NOT_A_ZIP", "Upload a ZIP archive of the repository.")
    return hashlib.sha256(data).hexdigest()


async def receive(
    db: AsyncSession,
    store: ObjectStore,
    settings: Settings,
    member: Member,
    project_id: UUID,
    data: bytes,
) -> dict[str, Any]:
    """Store an uploaded archive as the project's next snapshot and queue its acquisition."""
    sha = check_archive(data)
    if not await is_upload(db, member.workspace_id, project_id):
        raise ApiError(
            409, "NOT_AN_UPLOAD_PROJECT", "This project reads from GitHub; take a snapshot instead."
        )
    snapshot = await estimates.upsert_snapshot(db, member, project_id, sha, UPLOAD_REF)
    await db.commit()
    if snapshot.status in ("pending", "failed"):
        key = upload_key(member.workspace_id, project_id, snapshot.id)
        await store.put(key, data, "application/zip")  # no transaction is open during storage I/O
        await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
        await estimates._admit_acquisition(db, settings, member, snapshot.id)
        await db.execute(
            text(
                "UPDATE snapshots SET archive_key = :k, status = 'pending', rejection_code = NULL, updated_at = now()"
                " WHERE workspace_id = :w AND id = :s AND status IN ('pending', 'failed')"
            ),
            {"k": key, "w": member.workspace_id, "s": snapshot.id},
        )
        await jobs.create_snapshot_task(db, member.workspace_id, snapshot.id)
    else:
        await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    await audit(db, member, "snapshot.uploaded", "snapshot", snapshot.id, {"sha256": sha, "bytes": len(data)})
    await db.commit()
    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    return await estimates.get_snapshot(db, member, project_id, snapshot.id)


async def latest_scanned(db: AsyncSession, member: Member, project_id: UUID) -> Any:
    row = (
        await db.execute(
            text(
                "SELECT id, status, commit_sha FROM snapshots WHERE workspace_id = :w AND project_id = :p"
                " AND status = 'scanned' ORDER BY created_at DESC, id DESC LIMIT 1"
            ),
            {"w": member.workspace_id, "p": project_id},
        )
    ).one_or_none()
    if row is None:
        raise ApiError(409, "NO_UPLOAD", "Upload an archive and wait for its scan before building.")
    return row
