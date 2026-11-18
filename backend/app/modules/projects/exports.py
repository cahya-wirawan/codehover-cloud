"""Export requests and authenticated downloads (06 "/builds/{bid}/exports", "/exports/{eid}", 03, 10).

An editor requests an export of a ready build; a worker builds it. Only the requester or a workspace
admin may see or download it, every download is authorized again (no bearer URL), and a package
expires 24 hours after it is ready (410). At most 10 exports per workspace and hour (06 quotas).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.tenancy import set_context
from app.jobs import store as jobs
from app.modules.workspaces.access import Member
from app.modules.workspaces.service import audit

PER_HOUR = 10
COLUMNS = (
    "e.id, e.build_id, e.format, e.status, e.byte_count, e.error_code, e.created_at, e.completed_at, e.expires_at,"
    " (e.status = 'expired' OR (e.expires_at IS NOT NULL AND e.expires_at <= now())) AS expired, e.requested_by, e.object_key, e.sha256"
)


async def request_export(db: AsyncSession, member: Member, build_id: UUID, fmt: str) -> dict[str, Any]:
    build = (
        await db.execute(
            text("SELECT status, content_purged_at FROM builds WHERE workspace_id = :w AND id = :b"),
            {"w": member.workspace_id, "b": build_id},
        )
    ).one_or_none()
    if build is None:
        raise ApiError(404, "NOT_FOUND", "Build not found.")
    if build.status != "ready":
        raise ApiError(409, "BUILD_NOT_READY", "Only a ready build can be exported.")
    if build.content_purged_at is not None:
        raise ApiError(
            409, "BUILD_EXPIRED", "Retention removed this build's content; publish or export a newer build."
        )
    await db.execute(
        text("SELECT 1 FROM workspaces WHERE id = :w FOR UPDATE"), {"w": member.workspace_id}
    )  # serializes the hourly quota per workspace
    recent = (
        await db.execute(
            text(
                "SELECT count(*) FROM exports WHERE workspace_id = :w AND created_at > now() - interval '1 hour'"
            ),
            {"w": member.workspace_id},
        )
    ).scalar_one()
    if recent >= PER_HOUR:
        await db.rollback()
        raise ApiError(429, "EXPORT_LIMIT", "This workspace reached its hourly export limit.", retryable=True)
    export_id = (
        await db.execute(
            text(
                "INSERT INTO exports(workspace_id, build_id, format, requested_by) VALUES (:w, :b, :f, :u) RETURNING id"
            ),
            {"w": member.workspace_id, "b": build_id, "f": fmt, "u": member.user_id},
        )
    ).scalar_one()
    await jobs.create_task(db, member.workspace_id, build_id, "export", str(export_id))
    await audit(
        db, member, "export.requested", "build", build_id, {"export_id": str(export_id), "format": fmt}
    )
    await db.commit()
    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    return await get_export(db, member, export_id)


async def _visible(db: AsyncSession, member: Member, export_id: UUID) -> Any:
    row = (
        await db.execute(
            text(f"SELECT {COLUMNS} FROM exports e WHERE e.workspace_id = :w AND e.id = :e"),  # noqa: S608 - fixed columns
            {"w": member.workspace_id, "e": export_id},
        )
    ).one_or_none()
    # Requester or admin (06); anyone else in the workspace gets the same 404 as a stranger.
    if row is None or (row.requested_by != member.user_id and not member.at_least("admin")):
        raise ApiError(404, "NOT_FOUND", "Export not found.")
    return row


async def get_export(db: AsyncSession, member: Member, export_id: UUID) -> dict[str, Any]:
    row = await _visible(db, member, export_id)
    result = dict(row._mapping)
    for private in ("requested_by", "object_key", "sha256"):
        result.pop(private)
    return result


async def download(db: AsyncSession, member: Member, export_id: UUID) -> tuple[str, str, str, Any]:
    """(object key, content type, file name, row): authorized again on every request."""
    row = await _visible(db, member, export_id)
    if row.status == "expired":  # retention removed the package (0016)
        raise ApiError(410, "EXPORT_EXPIRED", "This export expired; request a new one.")
    if row.status != "ready":
        raise ApiError(
            409, "EXPORT_NOT_READY", "This export is not ready.", retryable=row.status == "pending"
        )
    if row.expired:
        raise ApiError(410, "EXPORT_EXPIRED", "This export expired; request a new one.")
    meta = (
        await db.execute(
            text(
                "SELECT p.slug, s.commit_sha FROM builds b JOIN projects p ON p.workspace_id = b.workspace_id AND p.id = b.project_id"
                " JOIN snapshots s ON s.workspace_id = b.workspace_id AND s.id = b.snapshot_id WHERE b.workspace_id = :w AND b.id = :b"
            ),
            {"w": member.workspace_id, "b": row.build_id},
        )
    ).one()
    await audit(db, member, "export.downloaded", "build", row.build_id, {"export_id": str(export_id)})
    await db.commit()
    zip_format = row.format == "static_zip"
    name = f"codehover-{meta.slug}-{meta.commit_sha[:12]}{'.zip' if zip_format else '-kb.json'}"
    return row.object_key, "application/zip" if zip_format else "application/json", name, row
