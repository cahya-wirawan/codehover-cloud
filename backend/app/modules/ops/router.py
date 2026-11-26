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

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import exc, text

from app.core.errors import ApiError
from app.core.settings import Settings
from app.core.tenancy import set_context
from app.modules.billing.plans import HOLDS_PLAN, PLANS
from app.modules.identity.sessions import CsrfPrincipal, CurrentPrincipal, Db, Principal
from app.modules.projects import service as projects
from app.modules.workspaces.service import normalize_email

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
    kind: str = "manual"  # manual, trial or credit (0044)


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


# ---------------------------------------------------------------- people and owners (0044)
# Handles, IDs, states, counts and money only: never names or emails. An exact email finds a person
# (POST, so it stays out of URLs and logs) without being shown.
PersonStatus = Annotated[str, Field(pattern="^(active|disabled)$")]


class PersonOut(BaseModel):
    user_id: UUID
    handle: str | None
    status: str
    created_at: datetime
    disabled_at: datetime | None
    last_active_at: datetime | None
    workspaces: int
    organizations: int
    builds_this_month: int
    spent_this_month_micro_usd: int
    operator: bool


class PersonList(BaseModel):
    items: list[PersonOut]
    next_cursor: str | None


class PersonWorkspace(BaseModel):
    workspace_id: UUID
    owner_handle: str | None
    owner_kind: str | None
    role: str
    via_organization: bool
    builds_this_month: int
    spent_this_month_micro_usd: int


class PersonOrganization(BaseModel):
    organization_id: UUID
    handle: str | None
    role: str


class PersonDetail(BaseModel):
    user_id: UUID
    handle: str | None
    status: str
    created_at: datetime
    disabled_at: datetime | None
    last_active_at: datetime | None
    operator: bool
    workspaces: list[PersonWorkspace]
    organizations: list[PersonOrganization]
    sign_in: list[str]
    live_sessions: int
    live_tokens: int
    cancelled_builds: int = 0  # set by a suspension: builds stopped by it


class LookupIn(BaseModel):
    email: Annotated[str, Field(min_length=3, max_length=254)]


def _people_cursor(rows: list[PersonOut], limit: int) -> str | None:
    if len(rows) < limit:
        return None
    last = rows[-1]
    return f"{last.created_at.isoformat()}|{last.user_id}"


@router.get("/people", response_model=PersonList)
async def people(
    db: Db,
    _: OperatorReader,
    handle: Annotated[str | None, Query(max_length=40)] = None,
    status: Annotated[PersonStatus | None, Query()] = None,
    cursor: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> PersonList:
    """Newest first. `cursor` is the last row's `created_at|user_id` from the previous page."""
    after: tuple[datetime, UUID] | tuple[None, None] = (None, None)
    if cursor:
        try:
            created, _sep, uid = cursor.partition("|")
            after = (datetime.fromisoformat(created), UUID(uid))
        except ValueError as error:
            raise ApiError(422, "INVALID_CURSOR", "The cursor is not valid.") from error
    rows = [
        PersonOut.model_validate(dict(r._mapping))
        for r in await db.execute(
            text("SELECT * FROM app_private.ops_people(:h, NULL, :s, :t, :i, :n)"),
            {"h": handle, "s": status, "t": after[0], "i": after[1], "n": limit},
        )
    ]
    return PersonList(items=rows, next_cursor=_people_cursor(rows, limit))


@router.post("/people/lookup", response_model=PersonList)
async def lookup_person(body: LookupIn, db: Db, _: OperatorWriter) -> PersonList:
    """People whose account or sign-in email is exactly this one (accounts are never merged by email,
    so there may be several). The email is matched, never returned."""
    rows = [
        PersonOut.model_validate(dict(r._mapping))
        for r in await db.execute(
            text("SELECT * FROM app_private.ops_people(NULL, :e, NULL, NULL, NULL, 20)"),
            {"e": normalize_email(body.email)},
        )
    ]
    await db.commit()
    return PersonList(items=rows, next_cursor=None)


async def _person(db: Db, user_id: UUID) -> PersonDetail:
    data = (await db.execute(text("SELECT app_private.ops_person(:u)"), {"u": user_id})).scalar_one()
    if data is None:
        raise ApiError(404, "NOT_FOUND", "Person not found.")
    return PersonDetail.model_validate(data)


@router.get("/people/{user_id}", response_model=PersonDetail)
async def person(user_id: UUID, db: Db, _: OperatorReader) -> PersonDetail:
    return await _person(db, user_id)


async def _set_status(db: Db, user_id: UUID, suspend: bool, reason: str) -> list[tuple[UUID, UUID]]:
    try:
        rows = list(
            await db.execute(
                text("SELECT workspace_id, build_id FROM app_private.ops_set_person_status(:u, :s, :r)"),
                {"u": user_id, "s": suspend, "r": reason},
            )
        )
    except exc.DBAPIError as error:
        await db.rollback()
        state = getattr(error.orig, "sqlstate", None)
        if state == "P0002":
            raise ApiError(404, "NOT_FOUND", "Person not found.") from error
        if state == "55000":
            raise ApiError(
                409, "OPERATOR_TARGET", "Operators cannot be suspended here; revoke the operator role first."
            ) from error
        raise
    await db.commit()
    return [(r.workspace_id, r.build_id) for r in rows]


@router.post("/people/{user_id}/suspension", response_model=PersonDetail)
async def suspend_person(user_id: UUID, body: SuspensionIn, db: Db, operator: OperatorWriter) -> PersonDetail:
    """The person cannot sign in; their sessions, access tokens and MCP sign-ins end, and their queued or
    running builds are cancelled. Their workspaces and sites stay up. Repeating it is harmless."""
    builds = await _set_status(db, user_id, True, body.reason)
    cancelled = 0
    for workspace_id, build_id in builds:
        # The workspace comes from the database (the suspended person's own builds), not from the request.
        await set_context(db, workspace_id=workspace_id, user_id=operator.user_id)
        cancelled += await projects.cancel_for_suspension(db, workspace_id, build_id, operator.user_id)
        await db.commit()
    await set_context(db, user_id=operator.user_id)
    detail = await _person(db, user_id)
    detail.cancelled_builds = cancelled
    return detail


@router.delete("/people/{user_id}/suspension", status_code=204)
async def reinstate_person(
    user_id: UUID, db: Db, _: OperatorWriter, reason: Annotated[str, Query(min_length=3, max_length=500)]
) -> None:
    """The person can sign in again. Ended sessions, tokens and MCP sign-ins stay ended."""
    await _set_status(db, user_id, False, reason)


class OwnerOut(BaseModel):
    namespace_id: UUID
    handle: str
    kind: str
    status: str
    workspaces: int
    projects: int
    published_sites: int
    builds_this_month: int
    allowance_micro_usd: int
    settled_micro_usd: int
    reserved_micro_usd: int
    granted_micro_usd: int
    plan_key: str | None


class OwnerList(BaseModel):
    items: list[OwnerOut]
    next_cursor: str | None


@router.get("/owners", response_model=OwnerList)
async def owners(
    request: Request,
    db: Db,
    _: OperatorReader,
    handle: Annotated[str | None, Query(max_length=40)] = None,
    cursor: Annotated[str | None, Query(max_length=40)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> OwnerList:
    """People and organizations by handle, with this month's allowance (plan or default, plus grants)
    and spending across their workspaces. `cursor` is the last row's handle."""
    settings: Settings = request.app.state.settings
    items = []
    for r in await db.execute(
        text("SELECT * FROM app_private.ops_owners(:h, :c, :n, :held)"),
        {"h": handle, "c": cursor, "n": limit, "held": list(HOLDS_PLAN)},
    ):
        row = dict(r._mapping)
        plan = row["plan_key"]
        base = PLANS[plan].allowance_micro_usd if plan in PLANS else settings.generation_allowance_micro_usd
        items.append(OwnerOut(**row, allowance_micro_usd=base + int(row["granted_micro_usd"])))
    return OwnerList(items=items, next_cursor=items[-1].handle if len(items) == limit else None)


@router.post("/owners/{namespace_id}/credit", response_model=EntitlementOut, status_code=201)
async def credit_owner(namespace_id: UUID, body: SuspensionIn, db: Db, _: OperatorWriter) -> EntitlementOut:
    """Hand back what the owner settled this month: a grant of that amount until the month ends,
    recorded in the workspace it goes through. The ledger keeps the real spending (22)."""
    try:
        row = (
            await db.execute(
                text("SELECT * FROM app_private.ops_credit_owner(:n, :r)"),
                {"n": namespace_id, "r": body.reason},
            )
        ).one()
    except exc.DBAPIError as error:
        await db.rollback()
        state = getattr(error.orig, "sqlstate", None)
        if state == "P0002":
            raise ApiError(404, "NOT_FOUND", "Owner not found.") from error
        if state == "55000":
            raise ApiError(409, "NOTHING_TO_CREDIT", "This owner has settled nothing this month.") from error
        raise
    await db.commit()
    return EntitlementOut.model_validate(dict(row._mapping))
