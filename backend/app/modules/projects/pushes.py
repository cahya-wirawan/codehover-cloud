"""Push rebuilds (M6, T21; 01 FR-025): a push to a project's tracked branch, per the project's policy.

- `off` (default): nothing happens; freshness still shows that an update is available.
- `snapshot`: the new commit is acquired and scanned, ready for someone to estimate and build.
- `build`: the commit is built. A project whose latest ready build was generated reuses that build's
  approved configuration, re-estimated on the new commit; the build starts only if the estimate's high
  bound fits the project's push cap, otherwise the estimate waits for a person. A project without a
  generated build gets a free replay build.

Pushes are debounced: deliveries within PUSH_DEBOUNCE_SECONDS update one pending request (the latest
commit wins), which runs at most PUSH_MAX_WAIT_SECONDS after the first. A commit already built or
snapshotted is skipped. Work runs on behalf of the admin who set the policy and stops, turning the policy
off, if they are no longer an admin. Nothing a push starts is ever published (FR-025).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ApiError
from app.core.settings import Settings
from app.core.tenancy import set_context
from app.engine.catalog import Catalog
from app.engine.estimate import GenerationConfig
from app.jobs import store as jobs
from app.modules.identity.sessions import Principal
from app.modules.projects import estimates, service, uploads
from app.modules.workspaces.access import Member
from app.modules.workspaces.service import audit

logger = logging.getLogger(__name__)
SHA = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$")
BRANCH_PREFIX = "refs/heads/"
CLAIM_SECONDS = 120  # a claimed request is invisible to other workers this long
POLL_SECONDS = 30  # an acquiring request is checked again after this
RETRY_SECONDS = 60  # a request blocked by a temporary limit is tried again after this
GIVE_UP_SECONDS = 86_400  # a request still blocked after a day fails
# Refusals that pass with time (an active build, a limit, the kill switch) are retried; others end it.
RETRYABLE = frozenset({"BUILD_ACTIVE", "WORKSPACE_BUILD_LIMIT", "ACQUISITION_LIMIT", "BUILDS_PAUSED"})


# ---------------------------------------------------------------- recording (webhook)
def parse_push(payload: Any) -> tuple[int, int, str, str] | None:
    """(installation, repository, branch, commit) of a branch push, or None for anything else: tags,
    deleted branches, malformed payloads."""
    if not isinstance(payload, dict) or payload.get("deleted") is True:
        return None
    ref, sha = payload.get("ref"), payload.get("after")
    installation = (payload.get("installation") or {}).get("id")
    repository = (payload.get("repository") or {}).get("id")
    if not (isinstance(ref, str) and ref.startswith(BRANCH_PREFIX) and 0 < len(ref) <= 255):
        return None
    if not (isinstance(sha, str) and SHA.match(sha) and set(sha) != {"0"}):
        return None
    if not (isinstance(installation, int) and isinstance(repository, int)):
        return None
    return installation, repository, ref.removeprefix(BRANCH_PREFIX), sha


async def record_push(db: AsyncSession, settings: Settings, payload: Any) -> int:
    """Debounce a verified push into each affected project's pending request. Returns how many projects
    it concerns. The caller owns the transaction."""
    push = parse_push(payload)
    if push is None:
        return 0
    installation, repository, branch, sha = push
    targets = (
        await db.execute(
            text("SELECT workspace_id, project_id FROM app_private.push_targets(:i, :r, :b)"),
            {"i": installation, "r": repository, "b": branch},
        )
    ).all()
    for target in targets:
        await set_context(db, workspace_id=target.workspace_id)
        await db.execute(
            text(
                "INSERT INTO push_requests(workspace_id, project_id, commit_sha, ref, due_at)"
                " VALUES (:w, :p, :sha, :ref, now() + make_interval(secs => :debounce))"
                " ON CONFLICT (project_id) WHERE state = 'pending' DO UPDATE SET commit_sha = EXCLUDED.commit_sha,"
                " pushes = push_requests.pushes + 1, updated_at = now(),"
                " due_at = least(push_requests.first_received_at + make_interval(secs => :wait),"
                "                now() + make_interval(secs => :debounce))"
            ),
            {
                "w": target.workspace_id,
                "p": target.project_id,
                "sha": sha,
                "ref": branch,
                "debounce": settings.push_debounce_seconds,
                "wait": settings.push_max_wait_seconds,
            },
        )
    await set_context(db)
    return len(targets)


# ---------------------------------------------------------------- processing (worker)
@dataclass(frozen=True)
class PushRuntime:
    sessions: async_sessionmaker[AsyncSession]
    settings: Settings
    catalog: Catalog


def _acting_member(workspace_id: UUID, user_id: UUID, role: Any) -> Member:
    """The policy's admin, acting without a browser session: only `user_id` and the role are used by
    the services called here (audit actor, RLS user context, role checks)."""
    principal = Principal(
        user_id=user_id,
        session_id=UUID(int=0),
        session_token="",
        csrf_hash=b"",
        reauthenticated_at=datetime.fromtimestamp(0, UTC),
    )
    return Member(principal=principal, workspace_id=workspace_id, role=role)


async def _claim(db: AsyncSession, workspace_id: UUID, request_id: UUID) -> Any:
    await set_context(db, workspace_id=workspace_id)
    row = (
        await db.execute(
            text(
                "UPDATE push_requests q SET due_at = now() + make_interval(secs => :claim), updated_at = now()"
                " FROM projects p WHERE q.workspace_id = :w AND q.id = :id AND q.due_at <= now()"
                " AND q.state IN ('pending', 'acquiring') AND p.workspace_id = q.workspace_id AND p.id = q.project_id"
                " RETURNING q.id, q.project_id, q.commit_sha, q.ref, q.state, q.snapshot_id, q.first_received_at,"
                " p.push_policy, p.push_cap_micro_usd, p.push_policy_by, p.tracked_ref, p.deleted_at"
            ),
            {"w": workspace_id, "id": request_id, "claim": CLAIM_SECONDS},
        )
    ).one_or_none()
    await db.commit()
    return row


async def _finish(
    db: AsyncSession,
    workspace_id: UUID,
    request_id: UUID,
    state: str,
    outcome: str,
    *,
    snapshot_id: UUID | None = None,
    build_id: UUID | None = None,
    delay: int | None = None,
) -> None:
    await set_context(db, workspace_id=workspace_id)
    await db.execute(
        text(
            "UPDATE push_requests SET state = :s, outcome = :o, updated_at = now(),"
            " snapshot_id = COALESCE(:snap, snapshot_id), build_id = COALESCE(:b, build_id),"
            " due_at = CASE WHEN CAST(:delay AS integer) IS NULL THEN due_at"
            "   ELSE now() + make_interval(secs => CAST(:delay AS integer)) END"
            " WHERE workspace_id = :w AND id = :id"
        ),
        {
            "s": state,
            "o": outcome,
            "snap": snapshot_id,
            "b": build_id,
            "delay": delay,
            "w": workspace_id,
            "id": request_id,
        },
    )
    await db.commit()


async def _approver(db: AsyncSession, workspace_id: UUID, user_id: UUID | None) -> str | None:
    if user_id is None:
        return None
    role = (
        await db.execute(
            text("SELECT role FROM memberships WHERE workspace_id = :w AND user_id = :u"),
            {"w": workspace_id, "u": user_id},
        )
    ).scalar_one_or_none()
    return role if role in ("admin", "owner") else None


async def _disable(db: AsyncSession, workspace_id: UUID, project_id: UUID, user_id: UUID | None) -> None:
    """The admin who set the policy left or was demoted: the policy turns off (audited, no actor)."""
    await db.execute(
        text(
            "UPDATE projects SET push_policy = 'off', push_policy_version = push_policy_version + 1"
            " WHERE workspace_id = :w AND id = :p AND push_policy <> 'off'"
        ),
        {"w": workspace_id, "p": project_id},
    )
    await db.execute(
        text(
            "INSERT INTO audit_events(workspace_id, action, resource_type, resource_id, metadata)"
            " VALUES (:w, 'project.push_policy_disabled', 'project', :p, CAST(:m AS jsonb))"
        ),
        {"w": workspace_id, "p": project_id, "m": '{"reason": "policy_admin_lost"}'},
    )


async def _already_done(
    db: AsyncSession, workspace_id: UUID, project_id: UUID, sha: str, policy: str
) -> bool:
    """Deduplicate by commit: a live or ready build of it (any policy), or its snapshot (snapshot policy)."""
    built = (
        await db.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM builds b JOIN snapshots s ON s.workspace_id = b.workspace_id"
                " AND s.id = b.snapshot_id WHERE b.workspace_id = :w AND b.project_id = :p AND s.commit_sha = :sha"
                " AND b.status IN ('queued', 'running', 'ready'))"
            ),
            {"w": workspace_id, "p": project_id, "sha": sha},
        )
    ).scalar_one()
    if built or policy != "snapshot":
        return bool(built)
    snapshotted = (
        await db.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM snapshots WHERE workspace_id = :w AND project_id = :p"
                " AND commit_sha = :sha AND status IN ('pending', 'acquiring', 'scanned'))"
            ),
            {"w": workspace_id, "p": project_id, "sha": sha},
        )
    ).scalar_one()
    return bool(snapshotted)


async def _generation_config(db: AsyncSession, workspace_id: UUID, project_id: UUID) -> dict[str, Any] | None:
    """The approved configuration of the project's latest ready generated build, if any."""
    config = (
        await db.execute(
            text(
                "SELECT e.config FROM builds b JOIN estimates e ON e.workspace_id = b.workspace_id AND e.id = b.estimate_id"
                " WHERE b.workspace_id = :w AND b.project_id = :p AND b.status = 'ready'"
                " AND e.config->>'variant_of' IS NULL"  # variants follow their base, not new commits
                " ORDER BY b.created_at DESC, b.id DESC LIMIT 1"
            ),
            {"w": workspace_id, "p": project_id},
        )
    ).scalar_one_or_none()
    return dict(config) if isinstance(config, dict) else None


async def _snapshot(db: AsyncSession, rt: PushRuntime, member: Member, row: Any) -> Any:
    """Upsert the pushed commit's snapshot and queue its acquisition when it is not scanned yet."""
    snapshot = await estimates.upsert_snapshot(db, member, row.project_id, row.commit_sha, row.ref)
    if snapshot.status in ("pending", "failed"):
        await estimates._admit_acquisition(db, rt.settings, member, snapshot.id)
        await jobs.create_snapshot_task(db, member.workspace_id, snapshot.id)
        await db.execute(
            text(
                "UPDATE snapshots SET status = 'pending', rejection_code = NULL, updated_at = now()"
                " WHERE workspace_id = :w AND id = :s AND status = 'failed'"
            ),
            {"w": member.workspace_id, "s": snapshot.id},
        )
    await audit(
        db,
        member,
        "snapshot.requested",
        "snapshot",
        snapshot.id,
        {"commit_sha": row.commit_sha, "trigger": "push"},
    )
    await db.commit()
    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    return snapshot


async def _replay_build(db: AsyncSession, rt: PushRuntime, member: Member, row: Any) -> UUID:
    snapshot = await estimates.upsert_snapshot(db, member, row.project_id, row.commit_sha, row.ref)
    if snapshot.status == "rejected":
        await db.rollback()
        raise ApiError(422, "SNAPSHOT_REJECTED", "This commit was rejected by the safety scan.")
    build = await service.start_replay_build_for(
        db,
        rt.settings,
        member,
        row.project_id,
        snapshot,
        ref=row.ref,
        commit_sha=row.commit_sha,
        trigger="push",
    )
    return UUID(str(build["id"]))


async def _generate(
    db: AsyncSession, rt: PushRuntime, member: Member, row: Any, snapshot_id: UUID, config: dict[str, Any]
) -> tuple[str, str, UUID | None]:
    """Estimate on the scanned commit; start the build when the estimate fits the push cap."""
    try:
        parsed = GenerationConfig.model_validate(config)
    except ValueError:
        return "skipped", "config_invalid", None
    estimate = await estimates.create_estimate(
        db, rt.catalog, rt.settings, member, row.project_id, snapshot_id, parsed
    )
    high = int(estimate["cost_micro_usd"]["high"])
    if high > row.push_cap_micro_usd:
        # The estimate waits for a person to approve it on the project page.
        await audit(
            db,
            member,
            "build.push_needs_approval",
            "estimate",
            UUID(str(estimate["id"])),
            {"high_micro_usd": high, "push_cap_micro_usd": row.push_cap_micro_usd},
        )
        await db.commit()
        await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
        return "skipped", "over_push_cap", None
    build = await service.start_generation_build(
        db, rt.catalog, rt.settings, member, row.project_id, UUID(str(estimate["id"])), None
    )
    return "done", "build_started", UUID(str(build["id"]))


async def process_request(rt: PushRuntime, workspace_id: UUID, request_id: UUID) -> str:
    async with rt.sessions() as db:
        row = await _claim(db, workspace_id, request_id)
        if row is None:
            return "claimed_elsewhere"
        await set_context(db, workspace_id=workspace_id)  # the claim's commit cleared it
        role = await _approver(db, workspace_id, row.push_policy_by)
        if row.deleted_at is not None or row.push_policy == "off" or row.ref != row.tracked_ref:
            await _finish(db, workspace_id, row.id, "skipped", "policy_off")
            return "policy_off"
        if role is None:
            await _disable(db, workspace_id, row.project_id, row.push_policy_by)
            await db.commit()
            await _finish(db, workspace_id, row.id, "skipped", "policy_admin_lost")
            return "policy_admin_lost"
        member = _acting_member(workspace_id, row.push_policy_by, role)
        await set_context(db, workspace_id=workspace_id, user_id=member.user_id)
        try:
            outcome = await _advance(db, rt, member, row)
        except ApiError as error:
            await db.rollback()
            waited = (datetime.now(UTC) - row.first_received_at).total_seconds()
            if error.code in RETRYABLE and waited < GIVE_UP_SECONDS:
                await _finish(db, workspace_id, row.id, row.state, error.code.lower(), delay=RETRY_SECONDS)
                return "retry"
            final = "failed" if error.code in RETRYABLE else "skipped"
            await _finish(db, workspace_id, row.id, final, error.code.lower())
            return error.code.lower()
        state, code, snapshot_id, build_id, delay = outcome
        await _finish(
            db, workspace_id, row.id, state, code, snapshot_id=snapshot_id, build_id=build_id, delay=delay
        )
        return code


async def _advance(
    db: AsyncSession, rt: PushRuntime, member: Member, row: Any
) -> tuple[str, str, UUID | None, UUID | None, int | None]:
    """One step for one request: (state, outcome, snapshot, build, delay before the next check)."""
    w, p = member.workspace_id, row.project_id
    if row.state == "pending":
        in_flight = (
            await db.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM push_requests WHERE workspace_id = :w AND project_id = :p AND state = 'acquiring')"
                ),
                {"w": w, "p": p},
            )
        ).scalar_one()
        if in_flight:
            return "pending", "waiting_for_previous", None, None, POLL_SECONDS
        if await _already_done(db, w, p, row.commit_sha, row.push_policy):
            return "skipped", "already_built", None, None, None
        if row.push_policy == "snapshot":
            snapshot = await _snapshot(db, rt, member, row)
            return "done", "snapshot_requested", snapshot.id, None, None
        config = await _generation_config(db, w, p)
        if config is None:
            return "done", "build_started", None, await _replay_build(db, rt, member, row), None
        snapshot = await _snapshot(db, rt, member, row)
        if snapshot.status != "scanned":
            return "acquiring", "acquiring", snapshot.id, None, POLL_SECONDS
        state, code, build = await _generate(db, rt, member, row, snapshot.id, config)
        return state, code, snapshot.id, build, None
    # acquiring: wait for the scan, then estimate and build
    status = (
        await db.execute(
            text("SELECT status FROM snapshots WHERE workspace_id = :w AND id = :s"),
            {"w": w, "s": row.snapshot_id},
        )
    ).scalar_one_or_none()
    if status == "rejected":
        return "skipped", "snapshot_rejected", None, None, None
    if status in (None, "failed"):
        return "failed", "snapshot_failed", None, None, None
    if status != "scanned":
        waited = (datetime.now(UTC) - row.first_received_at).total_seconds()
        if waited >= GIVE_UP_SECONDS:
            return "failed", "snapshot_timeout", None, None, None
        return "acquiring", "acquiring", None, None, POLL_SECONDS
    config = await _generation_config(db, w, p)
    if config is None:
        return "done", "build_started", None, await _replay_build(db, rt, member, row), None
    state, code, build = await _generate(db, rt, member, row, row.snapshot_id, config)
    return state, code, None, build, None


async def process_due(rt: PushRuntime, limit: int = 50) -> dict[str, int]:
    async with rt.sessions() as db:
        due = (await db.execute(text("SELECT * FROM app_private.due_push_requests(:n)"), {"n": limit})).all()
        await db.commit()
    counts: dict[str, int] = {}
    for item in due:
        try:
            outcome = await process_request(rt, item.workspace_id, item.request_id)
        except Exception:
            logger.exception("push request failed", extra={"workspace_id": str(item.workspace_id)})
            outcome = "error"  # the claim expires and the request is tried again
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


# ---------------------------------------------------------------- policy and history (API)
def policy_etag(version: int) -> str:
    return f'"push-policy-{version}"'


async def get_policy(db: AsyncSession, member: Member, project_id: UUID) -> dict[str, Any]:
    row = (
        await db.execute(
            text(
                "SELECT p.push_policy, p.push_cap_micro_usd, p.push_policy_version, u.display_name AS set_by_name"
                " FROM projects p LEFT JOIN users u ON u.id = p.push_policy_by"
                " WHERE p.workspace_id = :w AND p.id = :p AND p.deleted_at IS NULL"
            ),
            {"w": member.workspace_id, "p": project_id},
        )
    ).one_or_none()
    await db.commit()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Project not found.")
    return {
        "policy": row.push_policy,
        "cap_micro_usd": row.push_cap_micro_usd,
        "set_by_name": row.set_by_name if row.push_policy != "off" else None,
        "version": row.push_policy_version,
    }


async def set_policy(
    db: AsyncSession, member: Member, project_id: UUID, policy: str, cap: int, if_match: str | None
) -> dict[str, Any]:
    """Admins choose; the chooser becomes the person push work runs on behalf of (09)."""
    current = (
        await db.execute(
            text(
                "SELECT push_policy_version FROM projects WHERE workspace_id = :w AND id = :p AND deleted_at IS NULL"
                " FOR UPDATE"
            ),
            {"w": member.workspace_id, "p": project_id},
        )
    ).scalar_one_or_none()
    if current is None:
        raise ApiError(404, "NOT_FOUND", "Project not found.")
    if if_match is None:
        raise ApiError(428, "PRECONDITION_REQUIRED", "Send If-Match with the current push policy ETag.")
    if policy != "off" and await uploads.is_upload(db, member.workspace_id, project_id):
        raise ApiError(409, "NOT_A_GITHUB_PROJECT", "Uploaded projects are not connected to pushes.")
    if if_match != policy_etag(current):
        raise ApiError(
            409, "VERSION_CONFLICT", "The push policy changed meanwhile.", details={"version": current}
        )
    await db.execute(
        text(
            "UPDATE projects SET push_policy = :pol, push_cap_micro_usd = :cap, push_policy_version = push_policy_version + 1,"
            " push_policy_by = CASE WHEN :pol = 'off' THEN push_policy_by ELSE CAST(:u AS uuid) END"
            " WHERE workspace_id = :w AND id = :p"
        ),
        {"pol": policy, "cap": cap, "u": member.user_id, "w": member.workspace_id, "p": project_id},
    )
    if policy == "off":  # a waiting request is dropped; one already acquiring finishes its step
        await db.execute(
            text(
                "UPDATE push_requests SET state = 'skipped', outcome = 'policy_off', updated_at = now()"
                " WHERE workspace_id = :w AND project_id = :p AND state = 'pending'"
            ),
            {"w": member.workspace_id, "p": project_id},
        )
    await audit(
        db,
        member,
        "project.push_policy_changed",
        "project",
        project_id,
        {"policy": policy, "cap_micro_usd": cap},
    )
    await db.commit()
    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    return await get_policy(db, member, project_id)


async def list_requests(db: AsyncSession, member: Member, project_id: UUID) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            text(
                "SELECT id, commit_sha, ref, pushes, state, outcome, snapshot_id, build_id, first_received_at,"
                " updated_at FROM push_requests WHERE workspace_id = :w AND project_id = :p"
                " ORDER BY first_received_at DESC, id DESC LIMIT 20"
            ),
            {"w": member.workspace_id, "p": project_id},
        )
    ).all()
    await db.commit()
    return [dict(r._mapping) for r in rows]
