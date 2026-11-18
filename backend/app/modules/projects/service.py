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

from app.adapters.github.app import GitHubApp
from app.adapters.storage import ObjectStore
from app.core.errors import ApiError
from app.core.settings import Settings
from app.core.tenancy import set_context
from app.engine import ENGINE_VERSION, GENERATION_PROMPT_VERSION, PROMPT_VERSION
from app.engine.catalog import Catalog
from app.engine.generate import CITATION
from app.jobs import store as jobs
from app.modules.projects import estimates, quality
from app.modules.usage import ledger
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
    db: AsyncSession,
    github: GitHubApp | None,
    settings: Settings,
    member: Member,
    project_id: UUID,
    ref: str | None,
) -> dict[str, Any]:
    """M1 replay build from a ref: no estimate, zero cap, cards derived from the source only."""
    requested, sha = await estimates.resolve_commit(db, github, member, project_id, ref)
    snapshot = await estimates.upsert_snapshot(db, member, project_id, sha, requested)
    if snapshot.status == "rejected":
        await db.rollback()
        raise ApiError(422, "SNAPSHOT_REJECTED", "This commit was rejected by the safety scan.")
    config = {"ref": requested, "generator": "replay-v1"}
    digest = hashlib.sha256(json.dumps({**config, "commit": sha}, sort_keys=True).encode()).hexdigest()
    await _admit(db, settings, member)
    build_id = await _insert_build(
        db,
        member,
        project_id,
        snapshot.id,
        config=config,
        digest=digest,
        prompt_version=PROMPT_VERSION,
        cap=0,
        estimate_id=None,
        account_id=None,
    )
    await _schedule(db, member, build_id, scanned=snapshot.status == "scanned", commit_sha=sha)
    return await get_build(db, member, build_id)


async def start_generation_build(
    db: AsyncSession,
    catalog: Catalog,
    settings: Settings,
    member: Member,
    project_id: UUID,
    estimate_id: UUID,
    cap: int | None,
) -> dict[str, Any]:
    """Build from an approved estimate: exact snapshot and scope, cap reserved once (11, A05, A08)."""
    approved = (
        await db.execute(
            text(
                "SELECT e.id, e.snapshot_id, e.config, e.config_digest, e.price_version, e.high_micro_usd,"
                " e.expires_at <= now() AS expired, s.status AS snapshot_status, s.commit_sha,"
                " r.access_revoked_at, c.status AS connection_status"
                " FROM estimates e JOIN snapshots s ON s.workspace_id = e.workspace_id AND s.id = e.snapshot_id"
                " JOIN projects p ON p.workspace_id = e.workspace_id AND p.id = e.project_id"
                " JOIN repositories r ON r.workspace_id = p.workspace_id AND r.id = p.repository_id"
                " JOIN repository_connections c ON c.workspace_id = r.workspace_id AND c.id = r.connection_id"
                " WHERE e.workspace_id = :w AND e.project_id = :p AND e.id = :e AND p.deleted_at IS NULL"
            ),
            {"w": member.workspace_id, "p": project_id, "e": estimate_id},
        )
    ).one_or_none()
    if approved is None:
        raise ApiError(404, "NOT_FOUND", "Estimate not found.")
    if approved.expired:
        raise ApiError(
            409,
            "ESTIMATE_EXPIRED",
            "Refresh this estimate before building.",
            details={"estimate_id": str(estimate_id)},
        )
    if approved.price_version != catalog.revision or not estimates.preset_available(
        catalog, approved.config["model_preset"], settings
    ):
        raise ApiError(
            409,
            "ESTIMATE_STALE",
            "Prices or presets changed since this estimate; refresh it.",
            details={"estimate_id": str(estimate_id)},
        )
    if approved.connection_status != "active" or approved.access_revoked_at is not None:
        raise ApiError(409, "CONNECTION_INACTIVE", "Reconnect GitHub to build this project.")
    if approved.snapshot_status != "scanned":
        raise ApiError(409, "SNAPSHOT_NOT_READY", "This snapshot is not available for building.")
    account = await _admit(db, settings, member)
    requested_cap = min(account.available_micro_usd, approved.high_micro_usd) if cap is None else cap
    if requested_cap > account.available_micro_usd:
        await db.rollback()
        raise ApiError(
            409,
            "ALLOWANCE_EXCEEDED",
            "The build cap is larger than the generation allowance left this period.",
            details={"available_micro_usd": account.available_micro_usd, "cap_micro_usd": requested_cap},
        )
    config = {
        "generation": approved.config,
        "catalog": catalog.revision,
        "estimate_id": str(estimate_id),
    }
    build_id = await _insert_build(
        db,
        member,
        project_id,
        approved.snapshot_id,
        config=config,
        digest=approved.config_digest,
        prompt_version=GENERATION_PROMPT_VERSION,
        cap=requested_cap,
        estimate_id=estimate_id,
        account_id=account.id,
    )
    await ledger.reserve_build(db, member.workspace_id, account, build_id, requested_cap)
    await _schedule(db, member, build_id, scanned=True, commit_sha=approved.commit_sha)
    return await get_build(db, member, build_id)


async def _admit(db: AsyncSession, settings: Settings, member: Member) -> ledger.Account:
    """Lock the period's usage account: serializes reservations and the workspace active-build limit."""
    account = await ledger.current_account(
        db, member.workspace_id, settings.generation_allowance_micro_usd, lock=True
    )
    active = (
        await db.execute(
            text("SELECT count(*) FROM builds WHERE workspace_id = :w AND status IN ('queued', 'running')"),
            {"w": member.workspace_id},
        )
    ).scalar_one()
    limit = settings.max_active_builds_per_workspace
    if active >= limit:
        await db.rollback()
        raise ApiError(
            409,
            "WORKSPACE_BUILD_LIMIT",
            "This workspace already has the maximum number of active builds.",
            details={"limit": limit},
        )
    return account


async def _insert_build(
    db: AsyncSession,
    member: Member,
    project_id: UUID,
    snapshot_id: UUID,
    *,
    config: dict[str, Any],
    digest: str,
    prompt_version: str,
    cap: int,
    estimate_id: UUID | None,
    account_id: UUID | None,
) -> UUID:
    try:
        build_id: UUID = (
            await db.execute(
                text(
                    "INSERT INTO builds(workspace_id, project_id, snapshot_id, estimate_id, config, config_digest,"
                    " engine_version, prompt_version, cap_micro_usd, usage_account_id, created_by)"
                    " VALUES (:w, :p, :s, :e, CAST(:c AS jsonb), :d, :ev, :pv, :cap, :a, :u) RETURNING id"
                ),
                {
                    "w": member.workspace_id,
                    "p": project_id,
                    "s": snapshot_id,
                    "e": estimate_id,
                    "c": json.dumps(config),
                    "d": digest,
                    "ev": ENGINE_VERSION,
                    "pv": prompt_version,
                    "cap": cap,
                    "a": account_id,
                    "u": member.user_id,
                },
            )
        ).scalar_one()
    except exc.IntegrityError as error:
        await db.rollback()
        raise ApiError(
            409, "BUILD_ACTIVE", "A build for this project is already queued or running."
        ) from error
    return build_id


async def _schedule(
    db: AsyncSession, member: Member, build_id: UUID, *, scanned: bool, commit_sha: str
) -> None:
    """First task, outbox row, events and audit in the build's transaction (04)."""
    if scanned:  # reuse the immutable snapshot; record the skipped stage
        await db.execute(
            text(
                "INSERT INTO build_tasks(workspace_id, build_id, stage, work_key, state)"
                " VALUES (:w, :b, 'acquire', 'build', 'skipped')"
            ),
            {"w": member.workspace_id, "b": build_id},
        )
    await jobs.create_task(db, member.workspace_id, build_id, "index" if scanned else "acquire", "build")
    await jobs.event(db, member.workspace_id, build_id, "build.queued", {"commit_sha": commit_sha})
    await audit(db, member, "build.requested", "build", build_id, {"commit_sha": commit_sha})
    await db.commit()
    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)


async def get_build(db: AsyncSession, member: Member, build_id: UUID) -> dict[str, Any]:
    row = (
        await db.execute(
            text(
                "SELECT b.id, b.project_id, b.snapshot_id, b.estimate_id, b.status, b.created_at, b.completed_at,"
                " b.engine_version, b.prompt_version, b.cap_micro_usd, b.reserved_micro_usd, b.settled_micro_usd,"
                " s.commit_sha, s.status AS snapshot_status, s.rejection_code"
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
            " ORDER BY created_at, id"
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
    build = dict(row._mapping)
    usage = {k: build.pop(k) for k in ("cap_micro_usd", "reserved_micro_usd", "settled_micro_usd")}
    return {
        **build,
        "usage": usage,
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
    explained = (
        await db.execute(
            text(
                "SELECT status, summary, chunks FROM file_explanations"
                " WHERE workspace_id = :w AND build_id = :b AND source_file_id = :f"
            ),
            {"w": member.workspace_id, "b": build_id, "f": file_id},
        )
    ).one_or_none()
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
        "explanation": None
        if explained is None
        else {
            "status": explained.status,
            "summary": explained.summary,
            "chunks": [
                {k: c[k] for k in ("start_line", "end_line", "incomplete", "summary", "notes", "status")}
                for c in explained.chunks
            ],
        },
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


# ---------------------------------------------------------------- guide
async def guide_index(
    db: AsyncSession, objects: ObjectStore, member: Member, build_id: UUID
) -> dict[str, Any]:
    await _ready_build(db, member, build_id)
    pages = await db.execute(
        text(
            "SELECT slug, title, ordinal, purpose, status, check_status, provenance FROM guide_pages"
            " WHERE workspace_id = :w AND build_id = :b ORDER BY ordinal"
        ),
        {"w": member.workspace_id, "b": build_id},
    )
    page_list = [dict(p._mapping) for p in pages]
    overview_key = (
        await db.execute(
            text(
                "SELECT object_key FROM artifacts WHERE workspace_id = :w AND build_id = :b AND kind = 'overview'"
            ),
            {"w": member.workspace_id, "b": build_id},
        )
    ).scalar_one_or_none()
    await db.commit()
    overview = json.loads(await objects.get(overview_key)) if overview_key else None
    return {"overview": overview, "pages": page_list}


async def guide_page(
    db: AsyncSession, objects: ObjectStore, member: Member, build_id: UUID, slug: str
) -> dict[str, Any]:
    build = await _ready_build(db, member, build_id)
    page = (
        await db.execute(
            text(
                "SELECT id, slug, title, ordinal, purpose, status, check_status, provenance, markdown_key FROM guide_pages"
                " WHERE workspace_id = :w AND build_id = :b AND slug = :s"
            ),
            {"w": member.workspace_id, "b": build_id, "s": slug},
        )
    ).one_or_none()
    if page is None or page.markdown_key is None:
        raise ApiError(404, "NOT_FOUND", "Guide page not found.")
    paths = {
        str(r.id): r.path
        for r in await db.execute(
            text("SELECT id, path FROM source_files WHERE workspace_id = :w AND snapshot_id = :s"),
            {"w": member.workspace_id, "s": build.snapshot_id},
        )
    }
    page_claims = await quality.page_claims(db, member, page.id)
    await db.commit()
    markdown = (await objects.get(page.markdown_key)).decode("utf-8")
    citations = [
        {"file_id": fid, "path": paths.get(fid), "start_line": int(a), "end_line": int(b)}
        for fid, a, b in sorted(set(CITATION.findall(markdown)), key=lambda c: (c[0], int(c[1])))
    ]
    result = dict(page._mapping)
    result.pop("markdown_key")
    result.pop("id")
    return {**result, "markdown": markdown, "citations": citations, "claims": page_claims}
