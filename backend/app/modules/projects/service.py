"""Projects, build start/status and the private viewer read model (06).

Every query names its workspace explicitly (05). Viewer content comes from object storage by keys the
server recorded, never from client input; ranges always refer to the requested immutable build.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from sqlalchemy import exc, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.github.app import GitHubApp, GitHubError
from app.adapters.github.public import PublicGitHub
from app.adapters.storage import ObjectStore
from app.core.errors import ApiError
from app.core.settings import Settings
from app.core.tenancy import set_context
from app.engine import ENGINE_VERSION, GENERATION_PROMPT_VERSION, PROMPT_VERSION, search
from app.engine.catalog import Catalog
from app.engine.estimate import GenerationConfig
from app.engine.generate import CITATION
from app.jobs import store as jobs
from app.modules.billing import service as billing
from app.modules.projects import estimates, quality, uploads
from app.modules.providers import keys as provider_keys
from app.modules.usage import ledger
from app.modules.workspaces.access import Member, Reader
from app.modules.workspaces.service import audit

MAX_LINES_PER_REQUEST = 500
TEXT_STATUSES = ("selected", "unsupported")


# ---------------------------------------------------------------- projects
async def create_project(
    db: AsyncSession,
    member: Member,
    repository_id: UUID,
    name: str,
    slug: str,
    tracked_ref: str,
    *,
    provider: str = "github",
) -> dict[str, Any]:
    granted = (
        await db.execute(
            text(
                "SELECT r.default_branch FROM repositories r JOIN repository_connections c"
                " ON c.workspace_id = r.workspace_id AND c.id = r.connection_id"
                " WHERE r.workspace_id = :w AND r.id = :r AND r.access_revoked_at IS NULL AND c.status = 'active'"
                " AND c.provider = :provider"
            ),
            {"w": member.workspace_id, "r": repository_id, "provider": provider},
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
    await audit(
        db,
        member,
        "project.created",
        "project",
        project_id,
        {"repository_id": str(repository_id), "source": provider},
    )
    await db.commit()
    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    return await get_project(db, member, project_id)


async def list_projects(db: AsyncSession, member: Member) -> list[dict[str, Any]]:
    rows = await db.execute(
        text(
            "SELECT p.id, p.workspace_id, p.name, p.slug, p.tracked_ref, r.owner, r.name AS repository,"
            " c.provider AS source, p.access"
            " FROM projects p JOIN repositories r ON r.workspace_id = p.workspace_id AND r.id = p.repository_id"
            " JOIN repository_connections c ON c.workspace_id = r.workspace_id AND c.id = r.connection_id"
            " WHERE p.workspace_id = :w AND p.deleted_at IS NULL"
            # Restricted projects the caller may not see are left out (0027; the row policy agrees).
            " AND (:support OR app_private.project_role(p.workspace_id, p.id, :u) IS NOT NULL)"
            " AND (NOT :support OR p.access = 'workspace')"
            " ORDER BY p.created_at, p.id"
        ),
        {"w": member.workspace_id, "u": member.user_id, "support": member.support},
    )
    return [dict(r._mapping) for r in rows]


async def get_project(db: AsyncSession, member: Member, project_id: UUID) -> dict[str, Any]:
    row = (
        await db.execute(
            text(
                "SELECT p.id, p.workspace_id, p.name, p.slug, p.tracked_ref, r.owner, r.name AS repository,"
                " c.provider AS source, p.access,"
                " COALESCE(app_private.project_role(p.workspace_id, p.id, :u), 'viewer') AS my_role"
                " FROM projects p JOIN repositories r ON r.workspace_id = p.workspace_id AND r.id = p.repository_id"
                " JOIN repository_connections c ON c.workspace_id = r.workspace_id AND c.id = r.connection_id"
                " WHERE p.workspace_id = :w AND p.id = :p AND p.deleted_at IS NULL"
            ),
            {"w": member.workspace_id, "p": project_id, "u": member.user_id},
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
    *,
    retry_of: UUID | None = None,
    public: PublicGitHub | None = None,
) -> dict[str, Any]:
    """M1 replay build from a ref: no estimate, zero cap, cards derived from the source only. An uploaded
    project (M6) builds its latest scanned upload; it has no refs."""
    if await uploads.is_upload(db, member.workspace_id, project_id):
        if ref:
            raise ApiError(422, "REF_NOT_FOUND", "Uploaded projects have no branches; upload a new archive.")
        latest = await uploads.latest_scanned(db, member, project_id)
        return await start_replay_build_for(
            db,
            settings,
            member,
            project_id,
            latest,
            ref=uploads.UPLOAD_REF,
            commit_sha=latest.commit_sha,
            retry_of=retry_of,
        )
    requested, sha = await estimates.resolve_commit(db, github, member, project_id, ref, public=public)
    snapshot = await estimates.upsert_snapshot(db, member, project_id, sha, requested)
    if snapshot.status == "rejected":
        await db.rollback()
        raise ApiError(422, "SNAPSHOT_REJECTED", "This commit was rejected by the safety scan.")
    return await start_replay_build_for(
        db, settings, member, project_id, snapshot, ref=requested, commit_sha=sha, retry_of=retry_of
    )


async def start_replay_build_for(
    db: AsyncSession,
    settings: Settings,
    member: Member,
    project_id: UUID,
    snapshot: Any,
    *,
    ref: str,
    commit_sha: str,
    retry_of: UUID | None = None,
    trigger: str | None = None,
) -> dict[str, Any]:
    """A replay build of an upserted, not rejected snapshot (manual starts and push rebuilds)."""
    sha = commit_sha
    config: dict[str, Any] = {"ref": ref, "generator": "replay-v1"}
    if trigger:
        config["trigger"] = trigger
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
        retry_of=retry_of,
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
    *,
    retry_of: UUID | None = None,
    funding: str = "allowance",
) -> dict[str, Any]:
    """Build from an approved estimate: exact snapshot and scope, cap reserved once (11, A05, A08).
    `funding: own_key` (M6, T24) calls the provider with the workspace's own key: the cap is required and
    held on the build only, and the plan allowance is neither checked nor used."""
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
    own = await provider_keys.active_providers(db, member.workspace_id)
    if funding == "own_key":
        # The key spends the owner's provider account with no allowance behind it: admins and owners only
        # (security review). Editors build on the plan allowance.
        if not member.at_least("admin"):
            raise ApiError(403, "FORBIDDEN", "Only workspace admins can build on the workspace's own key.")
        if cap is None:
            raise ApiError(422, "CAP_REQUIRED", "Set a spending cap for a build on your own key.")
        if not estimates.own_key_usable(catalog, approved.config["model_preset"], own):
            raise ApiError(
                409, "NO_PROVIDER_KEY", "Add this workspace's own key for the preset's provider first."
            )
    elif not estimates.preset_available(catalog, approved.config["model_preset"], settings):
        raise ApiError(
            409,
            "PRESET_UNAVAILABLE",
            "This preset's provider is not configured for the plan allowance; build on your own key.",
        )
    if approved.price_version != catalog.revision or not estimates.preset_available(
        catalog, approved.config["model_preset"], settings, own
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
    account = await _admit(db, settings, member, generation=True)
    requested_cap = min(account.available_micro_usd, approved.high_micro_usd) if cap is None else cap
    if funding != "own_key" and requested_cap > account.available_micro_usd:
        await db.rollback()
        raise ApiError(
            409,
            "ALLOWANCE_EXCEEDED",
            "The build cap is larger than the generation allowance left this period.",
            details={"available_micro_usd": account.available_micro_usd, "cap_micro_usd": requested_cap},
        )
    if requested_cap == 0 and approved.high_micro_usd > 0:
        # A priced preset's calls cannot fit a zero envelope: the build could only stop at once. A free
        # preset (the replay model, priced at zero) runs with a zero cap.
        await db.rollback()
        if cap is None and funding != "own_key":
            raise ApiError(
                409,
                "ALLOWANCE_EXCEEDED",
                "No generation allowance is left this period.",
                details={"available_micro_usd": 0, "cap_micro_usd": 0},
            )
        raise ApiError(
            422, "CAP_TOO_LOW", "Set a spending cap above zero; a zero cap pays for no model call."
        )
    generation = GenerationConfig.model_validate(approved.config)
    # Several languages (FR-026): this build writes the first; each further one is a variant build of it,
    # started now beside it with a share of the cap, so the whole family is reserved at once. Shares are
    # equal, or for translated variants (P07) in proportion to the estimate's high bounds.
    others = generation.family[1:] if generation.variant_of is None else []
    share = requested_cap // (1 + len(others))
    if others and generation.translate:
        tokens = (
            await db.execute(
                text("SELECT token_estimate FROM estimates WHERE workspace_id = :w AND id = :e"),
                {"w": member.workspace_id, "e": approved.id},
            )
        ).scalar_one()
        base_high = int(tokens["per_language"]["high"])
        each_high = int((tokens.get("per_translation") or tokens["per_language"])["high"])
        total_high = base_high + each_high * len(others)
        if total_high > 0:
            share = requested_cap * each_high // total_high
    build_id = await _start_one(
        db,
        member,
        project_id,
        approved,
        config=approved.config,
        digest=approved.config_digest,
        estimate_id=estimate_id,
        cap=requested_cap - share * len(others),
        catalog=catalog,
        account=account,
        funding=funding,
        retry_of=retry_of,
    )
    for language in others:
        variant = generation.for_language(language, str(build_id))
        variant_estimate, variant_digest = await estimates.variant_estimate(
            db, member, project_id, approved, variant, catalog
        )
        await _start_one(
            db,
            member,
            project_id,
            approved,
            config=variant.canonical(),
            digest=variant_digest,
            estimate_id=variant_estimate,
            cap=share,
            catalog=catalog,
            account=account,
            funding=funding,
            family=build_id,
        )
    return await get_build(db, member, build_id)


async def _start_one(
    db: AsyncSession,
    member: Member,
    project_id: UUID,
    approved: Any,
    *,
    config: dict[str, Any],
    digest: str,
    estimate_id: UUID,
    cap: int,
    catalog: Catalog,
    account: ledger.Account,
    funding: str,
    retry_of: UUID | None = None,
    family: UUID | None = None,
) -> UUID:
    """One generation build of an approved estimate: insert, reserve its cap, schedule its first task.
    `family` (server-set, never from a request) marks a variant started together with its base."""
    build_config: dict[str, Any] = {
        "generation": config,
        "catalog": catalog.revision,
        "estimate_id": str(estimate_id),
    }
    if funding == "own_key":
        build_config["funding"] = "own_key"
    if family is not None:
        build_config["family"] = str(family)
    build_id = await _insert_build(
        db,
        member,
        project_id,
        approved.snapshot_id,
        config=build_config,
        digest=digest,
        prompt_version=GENERATION_PROMPT_VERSION,
        cap=cap,
        estimate_id=estimate_id,
        account_id=None if funding == "own_key" else account.id,
        retry_of=retry_of,
    )
    if funding == "own_key":  # the envelope lives on the build only (no allowance account)
        await db.execute(
            text("UPDATE builds SET reserved_micro_usd = :n WHERE workspace_id = :w AND id = :b"),
            {"n": cap, "w": member.workspace_id, "b": build_id},
        )
    else:
        await ledger.reserve_build(db, member.workspace_id, account, build_id, cap)
    await _schedule(db, member, build_id, scanned=True, commit_sha=approved.commit_sha)
    return build_id


async def _admit(
    db: AsyncSession, settings: Settings, member: Member, *, generation: bool = False
) -> ledger.Account:
    """Lock the period's usage account: serializes reservations and the workspace active-build limit.
    Generation builds also need billing in good standing (11: not past due, no open dispute)."""
    paused = (await db.execute(text("SELECT builds_paused FROM platform_settings"))).scalar_one_or_none()
    if paused:  # the operator kill switch (22): reads stay available, new builds wait
        await db.rollback()
        raise ApiError(
            503, "BUILDS_PAUSED", "New builds are paused by the operators; reading works.", retryable=True
        )
    if generation:
        try:
            await billing.require_generation(db, member.workspace_id)
        except ApiError:
            await db.rollback()
            raise
    account = await ledger.current_account(
        db, member.workspace_id, settings.generation_allowance_micro_usd, lock=True
    )
    active = (
        await db.execute(
            # A family (a several-language build: its base and the variants started with it, FR-026) is one
            # active build here. Only the server sets `family`, on the variants it starts with their base;
            # a variant started on its own counts by itself.
            text(
                "SELECT count(DISTINCT COALESCE(config->>'family', CAST(id AS text))) FROM builds"
                " WHERE workspace_id = :w AND status IN ('queued', 'running')"
            ),
            {"w": member.workspace_id},
        )
    ).scalar_one()
    limit = await billing.active_build_limit(db, settings, member.workspace_id)
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
    retry_of: UUID | None = None,
) -> UUID:
    try:
        build_id: UUID = (
            await db.execute(
                text(
                    "INSERT INTO builds(workspace_id, project_id, snapshot_id, estimate_id, retry_of_id, config,"
                    " config_digest, engine_version, prompt_version, cap_micro_usd, usage_account_id, created_by)"
                    " VALUES (:w, :p, :s, :e, :r, CAST(:c AS jsonb), :d, :ev, :pv, :cap, :a, :u) RETURNING id"
                ),
                {
                    "w": member.workspace_id,
                    "p": project_id,
                    "s": snapshot_id,
                    "e": estimate_id,
                    "r": retry_of,
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
                "SELECT b.id, b.project_id, b.snapshot_id, b.estimate_id, b.retry_of_id, b.status, b.created_at,"
                " b.completed_at, b.cancel_requested_at, b.content_purged_at,"
                " b.engine_version, b.prompt_version, b.cap_micro_usd, b.reserved_micro_usd, b.settled_micro_usd,"
                " s.commit_sha, s.status AS snapshot_status, s.rejection_code, b.config->'generation' AS generation"
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
    generation = build.pop("generation") or {}
    return {
        **build,
        "language": (generation.get("languages") or ["en"])[0],
        "level": (generation.get("levels") or ["standard"])[0],
        "variant_of": generation.get("variant_of"),
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
async def _ready_build(db: AsyncSession, member: Reader, build_id: UUID) -> Any:
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


async def tree(db: AsyncSession, member: Reader, build_id: UUID, path: str) -> list[dict[str, Any]]:
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
    member: Reader,
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
            " card->>'summary' AS summary, ARRAY(SELECT p->>'name' FROM jsonb_array_elements("
            "CASE WHEN jsonb_typeof(card->'params') = 'array' THEN card->'params' ELSE '[]'::jsonb END) p"
            " WHERE p->>'name' IS NOT NULL) AS params FROM symbols"
            " WHERE workspace_id = :w AND build_id = :b AND source_file_id = :f ORDER BY start_line, start_column"
        ),
        {"w": member.workspace_id, "b": build_id, "f": file_id},
    )
    symbol_list = [dict(s._mapping) for s in symbols]
    first = max(1, start_line)
    window_end = min(end_line or first + MAX_LINES_PER_REQUEST - 1, first + MAX_LINES_PER_REQUEST - 1)
    edges = await db.execute(
        text(
            'SELECT e.line, e."column", e.resolution, e.ambiguity_group IS NOT NULL AS ambiguous,'
            " t.id, t.name, t.kind FROM reference_edges e JOIN symbols t"
            " ON t.workspace_id = e.workspace_id AND t.id = e.to_symbol_id"
            " WHERE e.workspace_id = :w AND e.build_id = :b AND e.source_file_id = :f"
            ' AND e.line BETWEEN :first AND :last ORDER BY e.line, e."column", t.name'
        ),
        {"w": member.workspace_id, "b": build_id, "f": file_id, "first": first, "last": window_end},
    )
    references: dict[tuple[int, int], dict[str, Any]] = {}
    for e in edges:
        at = references.setdefault(
            (e.line, e.column),
            {
                "line": e.line,
                "column": e.column,
                "targets": [],
                "resolution": e.resolution,
                "ambiguous": False,
            },
        )
        if all(t["id"] != e.id for t in at["targets"]):
            at["targets"].append({"id": e.id, "name": e.name, "kind": e.kind})
        at["ambiguous"] = at["ambiguous"] or e.ambiguous or len(at["targets"]) > 1
        if e.resolution == "index":
            at["resolution"] = "index"
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
    last = min(len(lines), window_end)
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
        "references": [r for r in references.values() if r["line"] <= last],
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


async def symbol_view(db: AsyncSession, member: Reader, build_id: UUID, symbol_id: UUID) -> dict[str, Any]:
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
                f"SELECT DISTINCT o.id, o.name, o.kind, f.path, f.id AS file_id, o.start_line, e.resolution,"  # noqa: S608 - fixed column names
                " e.ambiguity_group IS NOT NULL AS ambiguous"
                f" FROM reference_edges e JOIN symbols o ON o.workspace_id = e.workspace_id AND o.id = e.{other}"
                " JOIN source_files f ON f.workspace_id = o.workspace_id AND f.id = o.source_file_id"
                f" WHERE e.workspace_id = :w AND e.build_id = :b AND e.{column} = :s ORDER BY f.path, o.name LIMIT 50"
            ),
            {"w": member.workspace_id, "b": build_id, "s": symbol_id},
        )
        return [dict(r._mapping) for r in rows]

    guides = await db.execute(
        text(
            "SELECT g.slug, g.title FROM guide_pages g WHERE g.workspace_id = :w AND g.build_id = :b AND EXISTS ("
            " SELECT 1 FROM claim_checks c WHERE c.workspace_id = g.workspace_id AND c.guide_page_id = g.id"
            " AND c.current AND c.deterministic = 'valid' AND c.source_file_id = :f"
            " AND c.start_line <= :end AND c.end_line >= :start) ORDER BY g.ordinal"
        ),
        {"w": member.workspace_id, "b": build_id, "f": s.file_id, "start": s.start_line, "end": s.end_line},
    )
    return {
        **dict(s._mapping),
        "uses": await related("from_symbol_id", "to_symbol_id"),
        "used_by": await related("to_symbol_id", "from_symbol_id"),
        "guides": [dict(g._mapping) for g in guides],
    }


# ---------------------------------------------------------------- guide
async def guide_index(
    db: AsyncSession, objects: ObjectStore, member: Reader, build_id: UUID
) -> dict[str, Any]:
    build = await _ready_build(db, member, build_id)
    pages = await db.execute(
        text(
            "SELECT slug, title, section, ordinal, purpose, status, check_status, provenance FROM guide_pages"
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
    paths = {
        str(r.id): r.path
        for r in await db.execute(
            text("SELECT id, path FROM source_files WHERE workspace_id = :w AND snapshot_id = :s"),
            {"w": member.workspace_id, "s": build.snapshot_id},
        )
    }
    await db.commit()
    overview = json.loads(await objects.get(overview_key)) if overview_key else None
    if overview:  # the reading order names files by ID; the viewer shows their paths
        for item in overview.get("reading_order", []):
            item["path"] = paths.get(str(item.get("file_id")))
    return {"overview": overview, "pages": page_list}


async def guide_page(
    db: AsyncSession, objects: ObjectStore, member: Reader, build_id: UUID, slug: str
) -> dict[str, Any]:
    build = await _ready_build(db, member, build_id)
    page = (
        await db.execute(
            text(
                "SELECT id, slug, title, section, ordinal, purpose, status, check_status, provenance, markdown_key"
                " FROM guide_pages"
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
    mentions = await _mentioned_symbols(db, member, build_id, markdown)
    citations = [
        {"file_id": fid, "path": paths.get(fid), "start_line": int(a), "end_line": int(b)}
        for fid, a, b in sorted(set(CITATION.findall(markdown)), key=lambda c: (c[0], int(c[1])))
    ]
    result = dict(page._mapping)
    result.pop("markdown_key")
    result.pop("id")
    return {
        **result,
        "markdown": markdown,
        "citations": citations,
        "claims": page_claims,
        "symbols": mentions,
    }


MENTION = re.compile(r"`([A-Za-z_][\w]*(?:(?:\.|::)[A-Za-z_][\w]*)*)(?:\(\))?`")


async def _mentioned_symbols(
    db: AsyncSession, member: Reader, build_id: UUID, markdown: str
) -> list[dict[str, Any]]:
    """Definitions a page names in backticks (`json2bin`, `Parser.parse`, `parse()`), so the viewer can link
    them to the code. A name is matched by its last segment and must name exactly one definition of the
    build; an ambiguous name is not linked (07 "explicit ambiguity")."""
    names: dict[str, str] = {}
    for mention in MENTION.findall(markdown)[:200]:
        names.setdefault(mention, re.split(r"\.|::", mention)[-1])
    if not names:
        return []
    # A new transaction (the page text was read from object storage with none open): the RLS context is
    # transaction-local, so it is set again, from the already-authorized reader (read paths use only the
    # workspace).
    await set_context(db, workspace_id=member.workspace_id)
    rows = await db.execute(
        text(
            "SELECT y.id, y.name, y.kind, y.start_line, y.source_file_id AS file_id, y.symbol_key FROM symbols y"
            " WHERE y.workspace_id = :w AND y.build_id = :b AND y.name = ANY(:names)"
        ),
        {"w": member.workspace_id, "b": build_id, "names": sorted(set(names.values()))},
    )
    by_name: dict[str, list[Any]] = {}
    for row in rows:
        by_name.setdefault(row.name, []).append(row)
    await db.commit()
    found = []
    for mention, name in names.items():
        candidates = by_name.get(name, [])
        if "." in mention or "::" in mention:  # a qualified mention must match the definition's key
            qualified = mention.replace("::", ".")
            candidates = [c for c in candidates if c.symbol_key.replace("::", ".").endswith(qualified)]
        if len(candidates) == 1:
            c = candidates[0]
            found.append(
                {
                    "mention": mention,
                    "id": c.id,
                    "name": c.name,
                    "kind": c.kind,
                    "file_id": c.file_id,
                    "start_line": c.start_line,
                }
            )
    return found


# ---------------------------------------------------------------- cancel, retry, events
async def request_cancel(
    db: AsyncSession, member: Member, build_id: UUID, reason: str | None
) -> dict[str, Any]:
    """Idempotent (06). No new provider call starts after this; an in-flight call may still finish and
    is settled; the unspent envelope is released when the build stops (A10). Cancelling the base of a
    several-language build cancels its language variants too (FR-026): they were started by the same
    request and cannot finish without the base's plan. Cancelling one variant stops only that one."""
    build = (
        await db.execute(
            text(
                "SELECT status, cancel_requested_at FROM builds WHERE workspace_id = :w AND id = :b FOR UPDATE"
            ),
            {"w": member.workspace_id, "b": build_id},
        )
    ).one_or_none()
    if build is None:
        raise ApiError(404, "NOT_FOUND", "Build not found.")
    note = (reason or "")[:200]
    if build.status in ("queued", "running") and build.cancel_requested_at is None:
        await _cancel_one(db, member.workspace_id, build_id, {"reason": note})
        await audit(db, member, "build.cancel_requested", "build", build_id, {"reason": note})
    # Rows are locked base first, then variants by ID: the base's ID is the family's and the smallest.
    variants = list(
        (
            await db.execute(
                text(
                    "SELECT id FROM builds WHERE workspace_id = :w AND config->>'family' = :f"
                    " AND status IN ('queued', 'running') AND cancel_requested_at IS NULL ORDER BY id FOR UPDATE"
                ),
                {"w": member.workspace_id, "f": str(build_id)},
            )
        ).scalars()
    )
    for variant in variants:
        await _cancel_one(db, member.workspace_id, variant, {"reason": note, "with_base": True})
    await db.commit()
    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    return await get_build(db, member, build_id)


async def _cancel_one(db: AsyncSession, workspace_id: UUID, build_id: UUID, payload: dict[str, Any]) -> None:
    await db.execute(
        text(
            "UPDATE builds SET cancel_requested_at = now(), updated_at = now() WHERE workspace_id = :w AND id = :b"
        ),
        {"w": workspace_id, "b": build_id},
    )
    await jobs.event(db, workspace_id, build_id, "build.cancel_requested", payload)
    working = (
        await db.execute(
            text(
                "SELECT count(*) FROM build_tasks WHERE workspace_id = :w AND build_id = :b"
                " AND state = 'leased' AND lease_until > now()"
            ),
            {"w": workspace_id, "b": build_id},
        )
    ).scalar_one()
    if not working:  # nothing is running: stop now; otherwise the worker stops at its next step
        await jobs.cancel_build(db, workspace_id, build_id)


async def retry_build(
    db: AsyncSession,
    github: GitHubApp | None,
    catalog: Catalog,
    settings: Settings,
    member: Member,
    build_id: UUID,
    estimate_id: UUID | None,
    cap: int | None,
    *,
    public: PublicGitHub | None = None,
) -> dict[str, Any]:
    """A new build for the same snapshot (06): terminal builds never run again. Generation builds need a
    valid estimate for that snapshot; the tenant cache makes completed units free on the retry (A13)."""
    original = (
        await db.execute(
            text(
                "SELECT b.project_id, b.snapshot_id, b.estimate_id, b.status, b.config, b.cap_micro_usd,"
                " e.snapshot_id AS estimate_snapshot"
                " FROM builds b LEFT JOIN estimates e ON e.workspace_id = b.workspace_id AND e.id = :e"
                " WHERE b.workspace_id = :w AND b.id = :b"
            ),
            {"w": member.workspace_id, "b": build_id, "e": estimate_id},
        )
    ).one_or_none()
    if original is None:
        raise ApiError(404, "NOT_FOUND", "Build not found.")
    if original.status in ("queued", "running"):
        raise ApiError(409, "BUILD_ACTIVE", "This build is still running.")
    if "generation" not in original.config:
        await db.commit()
        snapshot = (
            await db.execute(
                text("SELECT id, status, commit_sha FROM snapshots WHERE workspace_id = :w AND id = :s"),
                {"w": member.workspace_id, "s": original.snapshot_id},
            )
        ).one()
        if await uploads.is_upload(db, member.workspace_id, original.project_id):  # no ref to resolve (M6)
            return await start_replay_build_for(
                db,
                settings,
                member,
                original.project_id,
                snapshot,
                ref=uploads.UPLOAD_REF,
                commit_sha=snapshot.commit_sha,
                retry_of=build_id,
            )
        return await start_build(
            db,
            github,
            settings,
            member,
            original.project_id,
            snapshot.commit_sha,
            retry_of=build_id,
            public=public,
        )
    chosen = estimate_id or original.estimate_id
    if estimate_id is not None and original.estimate_snapshot != original.snapshot_id:
        raise ApiError(422, "ESTIMATE_MISMATCH", "The estimate must be for this build's snapshot.")
    funding = original.config.get("funding", "allowance")
    if funding == "own_key" and cap is None:
        cap = int(original.cap_micro_usd)  # an own-key retry keeps its cap unless a new one is given
    return await start_generation_build(
        db, catalog, settings, member, original.project_id, chosen, cap, retry_of=build_id, funding=funding
    )


async def build_events(
    db: AsyncSession, member: Member, build_id: UUID, after: int, limit: int
) -> dict[str, Any]:
    """Polling fallback for build progress (06 "Server-sent events"); sequence numbers are the event IDs."""
    exists = (
        await db.execute(
            text("SELECT 1 FROM builds WHERE workspace_id = :w AND id = :b"),
            {"w": member.workspace_id, "b": build_id},
        )
    ).one_or_none()
    if exists is None:
        raise ApiError(404, "NOT_FOUND", "Build not found.")
    rows = await db.execute(
        text(
            "SELECT sequence, event_type, payload, created_at FROM build_events"
            " WHERE workspace_id = :w AND build_id = :b AND sequence > :after ORDER BY sequence LIMIT :n"
        ),
        {"w": member.workspace_id, "b": build_id, "after": after, "n": limit},
    )
    items = [dict(r._mapping) for r in rows]
    return {"items": items, "next_after": items[-1]["sequence"] if items else after}


# ---------------------------------------------------------------- search
async def search_build(
    db: AsyncSession, member: Reader, build_id: UUID, query: str, kind: str | None, limit: int
) -> list[dict[str, Any]]:
    """Build-scoped search (06): exact name, then name prefix, then weighted full text. The workspace and
    build filter come first; query text is sanitized into a prefix tsquery and never logged."""
    await _ready_build(db, member, build_id)
    lowered = query.strip().lower()
    tsq = search.tsquery(query)
    if not lowered:
        return []
    escaped = lowered.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = await db.execute(
        text(
            "SELECT d.kind, d.ref_id, d.name, d.path, d.line, d.symbol_kind, d.snippet,"
            " CASE WHEN d.name_lower = :lower THEN 'exact_name' WHEN d.name_lower LIKE :prefix ESCAPE '\\'"
            "   THEN 'name_prefix' ELSE 'text' END AS basis,"
            " CASE WHEN CAST(:tsq AS text) IS NULL THEN 0"
            "   ELSE ts_rank(d.document, to_tsquery('simple', CAST(:tsq AS text))) END AS score"
            " FROM search_documents d"
            " WHERE d.workspace_id = :w AND d.build_id = :b AND (CAST(:kind AS text) IS NULL OR d.kind = :kind)"
            " AND (d.name_lower = :lower OR d.name_lower LIKE :prefix ESCAPE '\\'"
            "   OR (CAST(:tsq AS text) IS NOT NULL AND d.document @@ to_tsquery('simple', CAST(:tsq AS text))))"
            " ORDER BY CASE WHEN d.name_lower = :lower THEN 0 WHEN d.name_lower LIKE :prefix ESCAPE '\\' THEN 1 ELSE 2 END,"
            " score DESC, d.name, d.path, d.id LIMIT :n"
        ),
        {
            "w": member.workspace_id,
            "b": build_id,
            "kind": kind,
            "lower": lowered,
            "prefix": escaped + "%",
            "tsq": tsq,
            "n": limit,
        },
    )
    return [dict(r._mapping) for r in rows]


# ---------------------------------------------------------------- freshness (FR-019)
OBSERVATION_TTL_SECONDS = 300  # repository authority is cached at most five minutes (09)


async def freshness(
    db: AsyncSession,
    github: GitHubApp | None,
    member: Member,
    project_id: UUID,
    *,
    public: PublicGitHub | None = None,
) -> dict[str, Any]:
    """Latest upstream SHA of the tracked ref versus the published and latest ready builds. When GitHub
    cannot answer, the state is `unknown` with the last observation, never a claim of being current."""
    row = (
        await db.execute(
            text(
                "SELECT p.tracked_ref, p.last_observed_sha, p.last_observed_at,"
                " p.last_observed_at > now() - make_interval(secs => :ttl) AS recent,"
                " r.provider_repo_id, r.access_revoked_at, c.installation_id, c.status, c.provider,"
                " (SELECT s.commit_sha FROM publications pub JOIN builds b ON b.workspace_id = pub.workspace_id AND b.id = pub.build_id"
                "   JOIN snapshots s ON s.workspace_id = b.workspace_id AND s.id = b.snapshot_id"
                "   WHERE pub.workspace_id = p.workspace_id AND pub.project_id = p.id) AS published_sha,"
                " (SELECT s.commit_sha FROM builds b JOIN snapshots s ON s.workspace_id = b.workspace_id AND s.id = b.snapshot_id"
                "   WHERE b.workspace_id = p.workspace_id AND b.project_id = p.id AND b.status = 'ready'"
                "   ORDER BY b.created_at DESC, b.id DESC LIMIT 1) AS latest_ready_sha"
                " FROM projects p JOIN repositories r ON r.workspace_id = p.workspace_id AND r.id = p.repository_id"
                " JOIN repository_connections c ON c.workspace_id = r.workspace_id AND c.id = r.connection_id"
                " WHERE p.workspace_id = :w AND p.id = :p AND p.deleted_at IS NULL"
            ),
            {"w": member.workspace_id, "p": project_id, "ttl": OBSERVATION_TTL_SECONDS},
        )
    ).one_or_none()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Project not found.")
    await db.commit()
    if row.provider == "upload":  # an uploaded archive has no upstream to compare with (M6)
        return {
            "tracked_ref": row.tracked_ref,
            "state": "not_tracked",
            "reason": None,
            "latest_sha": None,
            "last_observed_sha": None,
            "last_observed_at": None,
            "published_sha": row.published_sha,
            "latest_ready_sha": row.latest_ready_sha,
        }
    observed_sha, observed_at, reason = row.last_observed_sha, row.last_observed_at, None
    if not row.recent:
        client_missing = public is None if row.provider == "public" else github is None
        if row.status != "active" or row.access_revoked_at is not None or client_missing:
            reason = "repository_access_unavailable"
        else:
            try:  # no transaction is open during the GitHub call
                if row.provider == "public":
                    assert public is not None
                    observed_sha = await public.resolve_ref(row.provider_repo_id, row.tracked_ref)
                else:
                    assert github is not None
                    observed_sha = await github.resolve_ref(
                        row.installation_id, row.provider_repo_id, row.tracked_ref
                    )
            except GitHubError:
                reason = "github_unavailable"
            else:
                await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
                observed_at = (
                    await db.execute(
                        text(
                            "UPDATE projects SET last_observed_sha = :s, last_observed_at = now()"
                            " WHERE workspace_id = :w AND id = :p RETURNING last_observed_at"
                        ),
                        {"s": observed_sha, "w": member.workspace_id, "p": project_id},
                    )
                ).scalar_one()
                await db.commit()
    reference = row.published_sha or row.latest_ready_sha
    if reason is not None:
        state = "unknown"
    elif reference is None:
        state = "never_built"
    else:
        state = "current" if observed_sha == reference else "update_available"
    return {
        "tracked_ref": row.tracked_ref,
        "state": state,
        "reason": reason,
        "latest_sha": observed_sha if reason is None else None,
        "last_observed_sha": observed_sha,
        "last_observed_at": observed_at,
        "published_sha": row.published_sha,
        "latest_ready_sha": row.latest_ready_sha,
    }


async def build_variants(db: AsyncSession, member: Reader, build_id: UUID) -> list[dict[str, Any]]:
    """The ready variants of a build's family (FR-026): its base and every ready variant of that base,
    so a reader can switch language or level and stay on the same page or file."""
    family = (
        await db.execute(
            text(
                "SELECT COALESCE(CAST(b.config->'generation'->>'variant_of' AS uuid), b.id) AS base"
                " FROM builds b WHERE b.workspace_id = :w AND b.id = :b"
            ),
            {"w": member.workspace_id, "b": build_id},
        )
    ).scalar_one_or_none()
    if family is None:
        raise ApiError(404, "NOT_FOUND", "Build not found.")
    rows = await db.execute(
        text(
            "SELECT b.id, COALESCE(b.config->'generation'->'languages'->>0, 'en') AS language,"
            " COALESCE(b.config->'generation'->'levels'->>0, 'standard') AS level, b.id = :base AS base"
            " FROM builds b WHERE b.workspace_id = :w AND b.status = 'ready' AND b.content_purged_at IS NULL"
            " AND (b.id = :base OR b.config->'generation'->>'variant_of' = CAST(:base AS text))"
            " ORDER BY (b.id = :base) DESC, b.created_at"
        ),
        {"w": member.workspace_id, "base": family},
    )
    return [dict(r._mapping) for r in rows]
