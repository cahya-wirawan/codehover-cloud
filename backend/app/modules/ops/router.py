"""Operations endpoints (02 S21 `/ops`, 09 "operations role", 11 manual entitlements, 22 kill switch).

Only platform operators reach these; everyone else gets 404, as for a route that does not exist.
Operators see identifiers, states, counts, codes and money, never names, paths or content (12). Every
action runs through an `app_private.ops_*` function that re-checks the operator role in the database,
records an operator event and, when a workspace is affected, an audit event in that workspace (0018).
Reading a workspace's content needs that workspace's own support grant (see workspaces/access.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.errors import ApiError
from app.core.tenancy import set_context
from app.modules.identity.sessions import CsrfPrincipal, CurrentPrincipal, Db, Principal

router = APIRouter(prefix="/ops", tags=["operations"])

Reason = Annotated[str, Field(min_length=3, max_length=500)]


async def _require(db: Db, principal: Principal) -> Principal:
    if not (await db.execute(text("SELECT app_private.is_operator()"))).scalar_one():
        raise ApiError(404, "NOT_FOUND", "Not found.")
    return principal


async def operator_reader(db: Db, principal: CurrentPrincipal) -> Principal:
    return await _require(db, principal)


async def operator_writer(db: Db, principal: CsrfPrincipal) -> Principal:
    return await _require(db, principal)


OperatorReader = Annotated[Principal, Depends(operator_reader)]
OperatorWriter = Annotated[Principal, Depends(operator_writer)]


class Summary(BaseModel):
    queue: dict[str, float]
    builds_last_24h: dict[str, int]
    tasks_by_stage: dict[str, int]
    builds_paused: bool
    paused_reason: str
    paused_since: datetime | None


@router.get("/summary", response_model=Summary)
async def summary(db: Db, _: OperatorReader) -> Summary:
    data: dict[str, Any] = (await db.execute(text("SELECT app_private.ops_summary()"))).scalar_one()
    queue = (await db.execute(text("SELECT * FROM app_private.ops_gauges_for_operator()"))).one()
    platform = data["platform"]
    return Summary(
        queue={k: float(v or 0) for k, v in queue._mapping.items()},
        builds_last_24h=data["builds_last_24h"],
        tasks_by_stage=data["tasks_by_stage"],
        builds_paused=platform["builds_paused"],
        paused_reason=platform["paused_reason"],
        paused_since=platform["updated_at"] if platform["builds_paused"] else None,
    )


class PauseIn(BaseModel):
    paused: bool
    reason: Reason


@router.post("/builds-paused", response_model=Summary)
async def set_builds_paused(body: PauseIn, db: Db, operator: OperatorWriter) -> Summary:
    await db.execute(
        text("SELECT app_private.ops_set_builds_paused(:p, :r)"), {"p": body.paused, "r": body.reason}
    )
    await db.commit()  # the context is transaction-local: set it again for the reads below
    await set_context(db, user_id=operator.user_id)
    return await summary(db, operator)


class TaskDiagnostics(BaseModel):
    id: UUID
    stage: str
    state: str
    attempts: int
    lease_generation: int
    lease_until: datetime | None
    next_attempt_at: datetime | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime


class AttemptGroup(BaseModel):
    purpose: str
    state: str
    count: int
    settled_micro_usd: int
    error_codes: list[str]


class BuildDiagnostics(BaseModel):
    id: UUID
    workspace_id: UUID
    project_id: UUID
    status: str
    created_at: datetime
    completed_at: datetime | None
    cancel_requested_at: datetime | None
    engine_version: str
    prompt_version: str
    cap_micro_usd: int
    reserved_micro_usd: int
    settled_micro_usd: int
    tasks: list[TaskDiagnostics]
    provider_attempts: list[AttemptGroup]
    events: int


@router.get("/builds/{build_id}", response_model=BuildDiagnostics)
async def build_diagnostics(build_id: UUID, db: Db, _: OperatorReader) -> BuildDiagnostics:
    data = (await db.execute(text("SELECT app_private.ops_build(:b)"), {"b": build_id})).scalar_one()
    if data is None:
        raise ApiError(404, "NOT_FOUND", "Build not found.")
    return BuildDiagnostics.model_validate(data)


class SuspensionIn(BaseModel):
    reason: Reason


async def _suspension(db: Db, project_id: UUID, suspend: bool, reason: str) -> None:
    workspace = (
        await db.execute(
            text("SELECT app_private.ops_suspend_publication(:p, :s, :r)"),
            {"p": project_id, "s": suspend, "r": reason},
        )
    ).scalar_one()
    if workspace is None:
        await db.rollback()
        raise ApiError(404, "NOT_FOUND", "No publication to change.")
    await db.commit()


@router.post("/projects/{project_id}/suspension", status_code=204)
async def suspend_publication(project_id: UUID, body: SuspensionIn, db: Db, _: OperatorWriter) -> None:
    """Operator takedown: the site stops at once; admins cannot lift or unpublish it (0012)."""
    await _suspension(db, project_id, True, body.reason)


@router.delete("/projects/{project_id}/suspension", status_code=204)
async def reinstate_publication(
    project_id: UUID, db: Db, _: OperatorWriter, reason: Annotated[str, Query(min_length=3, max_length=500)]
) -> None:
    await _suspension(db, project_id, False, reason)


class EntitlementIn(BaseModel):
    allowance_micro_usd: Annotated[int, Field(gt=0, le=1_000_000_000)]
    days: Annotated[int, Field(ge=1, le=366)]
    reason: Reason


class EntitlementOut(BaseModel):
    id: UUID
    workspace_id: UUID
    allowance_micro_usd: int
    starts_at: datetime
    expires_at: datetime
    reason: str
    granted_by: UUID
    created_at: datetime
    revoked_at: datetime | None


class EntitlementList(BaseModel):
    items: list[EntitlementOut]
    next_cursor: None = None


@router.get("/workspaces/{workspace_id}/entitlements", response_model=EntitlementList)
async def entitlements(workspace_id: UUID, db: Db, _: OperatorReader) -> EntitlementList:
    rows = await db.execute(text("SELECT * FROM app_private.ops_entitlements(:w)"), {"w": workspace_id})
    return EntitlementList(items=[EntitlementOut.model_validate(dict(r._mapping)) for r in rows])


@router.post("/workspaces/{workspace_id}/entitlements", response_model=EntitlementOut, status_code=201)
async def grant_entitlement(
    workspace_id: UUID, body: EntitlementIn, db: Db, operator: OperatorWriter
) -> EntitlementOut:
    grant = (
        await db.execute(
            text("SELECT app_private.ops_grant_entitlement(:w, :a, :d, :r)"),
            {"w": workspace_id, "a": body.allowance_micro_usd, "d": body.days, "r": body.reason},
        )
    ).scalar_one()
    if grant is None:
        await db.rollback()
        raise ApiError(404, "NOT_FOUND", "Workspace not found.")
    await db.commit()  # the context is transaction-local: set it again for the reads below
    await set_context(db, user_id=operator.user_id)
    items = (await entitlements(workspace_id, db, operator)).items
    return next(i for i in items if i.id == grant)


@router.delete("/entitlements/{grant_id}", status_code=204)
async def revoke_entitlement(
    grant_id: UUID, db: Db, _: OperatorWriter, reason: Annotated[str, Query(min_length=3, max_length=500)]
) -> None:
    workspace = (
        await db.execute(
            text("SELECT app_private.ops_revoke_entitlement(:g, :r)"), {"g": grant_id, "r": reason}
        )
    ).scalar_one()
    if workspace is None:
        await db.rollback()
        raise ApiError(404, "NOT_FOUND", "Entitlement not found.")
    await db.commit()


class OperatorEvent(BaseModel):
    id: UUID
    operator_user_id: UUID
    action: str
    workspace_id: UUID | None
    target_id: UUID | None
    reason: str
    created_at: datetime


class OperatorEventList(BaseModel):
    items: list[OperatorEvent]
    next_cursor: str | None


@router.get("/events", response_model=OperatorEventList)
async def events(
    db: Db,
    _: OperatorReader,
    before: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> OperatorEventList:
    rows = [
        OperatorEvent.model_validate(dict(r._mapping))
        for r in await db.execute(
            text("SELECT * FROM app_private.ops_events(:b, :n)"), {"b": before, "n": limit}
        )
    ]
    # The cursor is the last event's time: pass it back as `before`.
    return OperatorEventList(
        items=rows, next_cursor=rows[-1].created_at.isoformat() if len(rows) == limit else None
    )


@router.delete("/slug-reservations/{slug}", status_code=204)
async def release_slug(
    slug: Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9-]{2,62}$")],
    db: Db,
    _: OperatorWriter,
    reason: Annotated[str, Query(min_length=3, max_length=500)],
) -> None:
    """End a released address's reservation early (0036), so any workspace may publish at it."""
    workspace = (
        await db.execute(text("SELECT app_private.ops_release_slug(:s, :r)"), {"s": slug, "r": reason})
    ).scalar_one()
    if workspace is None:
        await db.rollback()
        raise ApiError(404, "NOT_FOUND", "That address is not reserved.")
    await db.commit()
