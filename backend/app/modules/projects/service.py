"""Projects, build start/status and the private viewer read model (06).

Every query names its workspace explicitly (05). Viewer content comes from object storage by keys the
server recorded, never from client input; ranges always refer to the requested immutable build.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from sqlalchemy import exc, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.github.app import GitHubApp, GitHubError
from app.adapters.storage import ObjectStore
from app.core.errors import ApiError
from app.core.tenancy import set_context
from app.engine import ENGINE_VERSION, PROMPT_VERSION
from app.jobs import store as jobs
from app.modules.workspaces.access import Member
from app.modules.workspaces.service import audit

MAX_LINES_PER_REQUEST = 500
TEXT_STATUSES = ("selected", "unsupported")


# ---------------------------------------------------------------- projects
async def create_project(
    db: AsyncSession, member: Member, repository_id: UUID, name: str, slug: str, tracked_ref: str
) -> dict[str, Any]:
    granted = (
        await db.execute(
            text(
                "SELECT r.default_branch FROM repositories r JOIN repository_connections c"
                " ON c.workspace_id = r.workspace_id AND c.id = r.connection_id"
                " WHERE r.workspace_id = :w AND r.id = :r AND r.access_revoked_at IS NULL AND c.status = 'active'"
            ),
            {"w": member.workspace_id, "r": repository_id},
        )
    ).one_or_none()
    if granted is None:
        raise ApiError(404, "NOT_FOUND", "Repository not found or not granted.")
    try:
        project_id = (
            await db.execute(
                text(
                    "INSERT INTO projects(workspace_id, repository_id, name, slug, tracked_ref)"
                    " VALUES (:w, :r, :n, :s, :t) RETURNING id"
                ),
                {
                    "w": member.workspace_id,
                    "r": repository_id,
                    "n": name,
                    "s": slug,
                    "t": tracked_ref or granted.default_branch,
                },
            )
        ).scalar_one()
    except exc.IntegrityError as error:
        await db.rollback()
        raise ApiError(409, "SLUG_TAKEN", "A project with this address already exists.") from error
    await audit(db, member, "project.created", "project", project_id, {"repository_id": str(repository_id)})
    await db.commit()
    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    return await get_project(db, member, project_id)


async def list_projects(db: AsyncSession, member: Member) -> list[dict[str, Any]]:
    rows = await db.execute(
        text(
            "SELECT p.id, p.workspace_id, p.name, p.slug, p.tracked_ref, r.owner, r.name AS repository"
            " FROM projects p JOIN repositories r ON r.workspace_id = p.workspace_id AND r.id = p.repository_id"
            " WHERE p.workspace_id = :w AND p.deleted_at IS NULL ORDER BY p.created_at, p.id"
        ),
        {"w": member.workspace_id},
    )
    return [dict(r._mapping) for r in rows]


async def get_project(db: AsyncSession, member: Member, project_id: UUID) -> dict[str, Any]:
    row = (
        await db.execute(
            text(
                "SELECT p.id, p.workspace_id, p.name, p.slug, p.tracked_ref, r.owner, r.name AS repository"
                " FROM projects p JOIN repositories r ON r.workspace_id = p.workspace_id AND r.id = p.repository_id"
                " WHERE p.workspace_id = :w AND p.id = :p AND p.deleted_at IS NULL"
            ),
            {"w": member.workspace_id, "p": project_id},
        )
    ).one_or_none()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Project not found.")
    return dict(row._mapping)


# ---------------------------------------------------------------- builds
async def start_build(
    db: AsyncSession, github: GitHubApp | None, member: Member, project_id: UUID, ref: str | None
) -> dict[str, Any]:
    source = (
        await db.execute(
            text(
                "SELECT p.tracked_ref, r.provider_repo_id, r.access_revoked_at, c.installation_id, c.status"
                " FROM projects p JOIN repositories r ON r.workspace_id = p.workspace_id AND r.id = p.repository_id"
                " JOIN repository_connections c ON c.workspace_id = r.workspace_id AND c.id = r.connection_id"
                " WHERE p.workspace_id = :w AND p.id = :p AND p.deleted_at IS NULL"
            ),
            {"w": member.workspace_id, "p": project_id},
        )
    ).one_or_none()
    if source is None:
        raise ApiError(404, "NOT_FOUND", "Project not found.")
    await db.commit()
    if source.status != "active" or source.access_revoked_at is not None:
        raise ApiError(409, "CONNECTION_INACTIVE", "Reconnect GitHub to build this project.")
    if github is None:
        raise ApiError(503, "GITHUB_APP_UNAVAILABLE", "GitHub repository access is not configured.")
    requested = ref or source.tracked_ref
    try:  # no transaction open during the GitHub call
        sha = await github.resolve_ref(source.installation_id, source.provider_repo_id, requested)
    except GitHubError as error:
        if error.status in (404, 422):
            raise ApiError(422, "REF_NOT_FOUND", "That branch, tag or commit does not exist.") from error
        raise ApiError(503, "UNAVAILABLE", "GitHub is not reachable right now.", retryable=True) from error

    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    snapshot = (
        await db.execute(
            text(
                "INSERT INTO snapshots(workspace_id, project_id, commit_sha, observed_ref)"
                " VALUES (:w, :p, :sha, :ref) ON CONFLICT (project_id, commit_sha)"
                " DO UPDATE SET updated_at = now() RETURNING id, status"
            ),
            {"w": member.workspace_id, "p": project_id, "sha": sha, "ref": requested},
        )
    ).one()
    if snapshot.status == "rejected":
        await db.rollback()
        raise ApiError(422, "SNAPSHOT_REJECTED", "This commit was rejected by the safety scan.")
    config = {"ref": requested, "generator": "replay-v1"}
    digest = hashlib.sha256(json.dumps({**config, "commit": sha}, sort_keys=True).encode()).hexdigest()
    try:
        build_id = (
            await db.execute(
                text(
                    "INSERT INTO builds(workspace_id, project_id, snapshot_id, config, config_digest,"
                    " engine_version, prompt_version, cap_micro_usd, created_by)"
                    " VALUES (:w, :p, :s, CAST(:c AS jsonb), :d, :ev, :pv, 0, :u) RETURNING id"
                ),
                {
                    "w": member.workspace_id,
                    "p": project_id,
                    "s": snapshot.id,
                    "c": json.dumps(config),
                    "d": digest,
                    "ev": ENGINE_VERSION,
                    "pv": PROMPT_VERSION,
                    "u": member.user_id,
                },
            )
        ).scalar_one()
    except exc.IntegrityError as error:
        await db.rollback()
        raise ApiError(
            409, "BUILD_ACTIVE", "A build for this project is already queued or running."
        ) from error
    first_stage = "index" if snapshot.status == "scanned" else "acquire"
    if first_stage == "index":  # reuse the immutable snapshot; record the skipped stage
        await db.execute(
            text(
                "INSERT INTO build_tasks(workspace_id, build_id, stage, work_key, state)"
                " VALUES (:w, :b, 'acquire', 'build', 'skipped')"
            ),
            {"w": member.workspace_id, "b": build_id},
        )
    await jobs.create_task(db, member.workspace_id, build_id, first_stage, "build")
    await jobs.event(db, member.workspace_id, build_id, "build.queued", {"commit_sha": sha})
    await audit(db, member, "build.requested", "build", build_id, {"commit_sha": sha})
    await db.commit()
    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    return await get_build(db, member, build_id)


async def get_build(db: AsyncSession, member: Member, build_id: UUID) -> dict[str, Any]:
    row = (
        await db.execute(
            text(
                "SELECT b.id, b.project_id, b.status, b.created_at, b.completed_at, b.engine_version,"
                " b.prompt_version, s.commit_sha, s.status AS snapshot_status, s.rejection_code"
                " FROM builds b JOIN snapshots s ON s.workspace_id = b.workspace_id AND s.id = b.snapshot_id"
                " WHERE b.workspace_id = :w AND b.id = :b"
            ),
            {"w": member.workspace_id, "b": build_id},
        )
    ).one_or_none()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Build not found.")
    stages = await db.execute(
        text(
            "SELECT stage, state, error_code FROM build_tasks WHERE workspace_id = :w AND build_id = :b"
            " ORDER BY created_at"
        ),
        {"w": member.workspace_id, "b": build_id},
    )
    counts = (
        await db.execute(
            text(
                "SELECT"
                " (SELECT count(*) FROM source_files f JOIN builds b ON b.workspace_id = f.workspace_id"
                "   AND b.snapshot_id = f.snapshot_id WHERE b.workspace_id = :w AND b.id = :b) AS files,"
                " (SELECT count(*) FROM symbols y WHERE y.workspace_id = :w AND y.build_id = :b) AS symbols"
            ),
            {"w": member.workspace_id, "b": build_id},
        )
    ).one()
    return {
        **dict(row._mapping),
        "stages": [dict(s._mapping) for s in stages],
        "counts": {"files": counts.files, "symbols": counts.symbols},
    }


async def list_builds(db: AsyncSession, member: Member, project_id: UUID) -> list[dict[str, Any]]:
    rows = await db.execute(
        text(
            "SELECT b.id, b.status, b.created_at, b.completed_at, s.commit_sha FROM builds b"
            " JOIN snapshots s ON s.workspace_id = b.workspace_id AND s.id = b.snapshot_id"
            " WHERE b.workspace_id = :w AND b.project_id = :p ORDER BY b.created_at DESC, b.id LIMIT 50"
        ),
        {"w": member.workspace_id, "p": project_id},
    )
    return [dict(r._mapping) for r in rows]


# ---------------------------------------------------------------- viewer
async def _ready_build(db: AsyncSession, member: Member, build_id: UUID) -> Any:
    row = (
        await db.execute(
            text("SELECT id, snapshot_id, status FROM builds WHERE workspace_id = :w AND id = :b"),
            {"w": member.workspace_id, "b": build_id},
        )
    ).one_or_none()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Build not found.")
    if row.status != "ready":
        raise ApiError(409, "BUILD_NOT_READY", "This build is not ready yet.")
    return row


async def tree(db: AsyncSession, member: Member, build_id: UUID, path: str) -> list[dict[str, Any]]:
    build = await _ready_build(db, member, build_id)
    prefix = path.strip("/")
    rows = await db.execute(
        text(
            "SELECT id, path, language, scan_status, scan_reason FROM source_files"
            " WHERE workspace_id = :w AND snapshot_id = :s AND (:p = '' OR path LIKE :like ESCAPE '\\')"
            " ORDER BY path"
        ),
        {
            "w": member.workspace_id,
            "s": build.snapshot_id,
            "p": prefix,
            "like": prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%",
        },
    )
    entries: dict[str, dict[str, Any]] = {}
    depth = len(PurePosixPath(prefix).parts) if prefix else 0
    for r in rows:
        parts = PurePosixPath(r.path).parts
        name = parts[depth]
        if len(parts) > depth + 1:
            entries.setdefault(name, {"name": name, "path": "/".join(parts[: depth + 1]), "type": "dir"})
        else:
            entries[name] = {
                "name": name,
                "path": r.path,
                "type": "file",
                "file_id": r.id,
                "language": r.language,
                "status": r.scan_status,
                "reason": r.scan_reason,
            }
    return sorted(entries.values(), key=lambda e: (e["type"] != "dir", e["name"]))


async def file_view(
    db: AsyncSession,
    objects: ObjectStore,
    member: Member,
    build_id: UUID,
    file_id: UUID,
    start_line: int,
    end_line: int | None,
) -> dict[str, Any]:
    build = await _ready_build(db, member, build_id)
    f = (
        await db.execute(
            text(
                "SELECT id, path, language, sha256, blob_key, line_count, scan_status, scan_reason FROM source_files"
                " WHERE workspace_id = :w AND snapshot_id = :s AND id = :f"
            ),
            {"w": member.workspace_id, "s": build.snapshot_id, "f": file_id},
        )
    ).one_or_none()
    if f is None:
        raise ApiError(404, "NOT_FOUND", "File not found.")
    if f.scan_status not in TEXT_STATUSES or f.blob_key is None:
        raise ApiError(
            409,
            "FILE_UNAVAILABLE",
            "This file was not included in the build.",
            details={"status": f.scan_status, "reason": f.scan_reason},
        )
    symbols = await db.execute(
        text(
            "SELECT id, symbol_key, name, kind, start_line, end_line, start_column, end_column,"
            " card->>'summary' AS summary FROM symbols"
            " WHERE workspace_id = :w AND build_id = :b AND source_file_id = :f ORDER BY start_line, start_column"
        ),
        {"w": member.workspace_id, "b": build_id, "f": file_id},
    )
    symbol_list = [dict(s._mapping) for s in symbols]
    await db.commit()  # no transaction open while reading object storage
    lines = (await objects.get(f.blob_key)).decode("utf-8").split("\n")
    first = max(1, start_line)
    last = min(len(lines), end_line or first + MAX_LINES_PER_REQUEST - 1, first + MAX_LINES_PER_REQUEST - 1)
    return {
        "id": f.id,
        "path": f.path,
        "language": f.language,
        "sha256": f.sha256,
        "line_count": f.line_count,
        "start_line": first,
        "end_line": last,
        "lines": lines[first - 1 : last],
        "symbols": symbol_list,
    }


async def symbol_view(db: AsyncSession, member: Member, build_id: UUID, symbol_id: UUID) -> dict[str, Any]:
    await _ready_build(db, member, build_id)
    s = (
        await db.execute(
            text(
                "SELECT y.id, y.symbol_key, y.name, y.kind, y.signature, y.start_line, y.end_line, y.start_column,"
                " y.end_column, y.card, f.id AS file_id, f.path FROM symbols y JOIN source_files f"
                " ON f.workspace_id = y.workspace_id AND f.id = y.source_file_id"
                " WHERE y.workspace_id = :w AND y.build_id = :b AND y.id = :s"
            ),
            {"w": member.workspace_id, "b": build_id, "s": symbol_id},
        )
    ).one_or_none()
    if s is None:
        raise ApiError(404, "NOT_FOUND", "Symbol not found.")

    async def related(column: str, other: str) -> list[dict[str, Any]]:
        rows = await db.execute(
            text(
                f"SELECT DISTINCT o.id, o.name, o.kind, f.path, e.resolution, e.ambiguity_group IS NOT NULL AS ambiguous"  # noqa: S608 - fixed column names
                f" FROM reference_edges e JOIN symbols o ON o.workspace_id = e.workspace_id AND o.id = e.{other}"
                " JOIN source_files f ON f.workspace_id = o.workspace_id AND f.id = o.source_file_id"
                f" WHERE e.workspace_id = :w AND e.build_id = :b AND e.{column} = :s ORDER BY f.path, o.name LIMIT 50"
            ),
            {"w": member.workspace_id, "b": build_id, "s": symbol_id},
        )
        return [dict(r._mapping) for r in rows]

    return {
        **dict(s._mapping),
        "uses": await related("from_symbol_id", "to_symbol_id"),
        "used_by": await related("to_symbol_id", "from_symbol_id"),
    }
