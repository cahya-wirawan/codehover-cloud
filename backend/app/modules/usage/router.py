"""Usage summary, ledger and the approved model presets (06 "/workspaces/{wid}/usage", 11)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.pagination import Position, clamp_limit, decode_cursor, encode_cursor
from app.core.settings import Settings
from app.engine.catalog import Catalog
from app.modules.identity.sessions import CurrentPrincipal, Db
from app.modules.projects.estimates import batch_available, own_key_usable, preset_available
from app.modules.providers import keys as provider_keys
from app.modules.usage import ledger, limits
from app.modules.workspaces.access import ReadMember, WriteMember, read_member, require_role

router = APIRouter(tags=["usage"])


class UsageOut(BaseModel):
    period_start: datetime
    period_end: datetime
    allowance_micro_usd: int
    reserved_micro_usd: int
    settled_micro_usd: int
    available_micro_usd: int
    # The caller's own monthly limit in this workspace and what their builds hold or settled (0044).
    member_limit_micro_usd: int | None = None
    member_used_micro_usd: int = 0


class MemberUsage(BaseModel):
    user_id: UUID
    display_name: str
    handle: str | None
    role: str
    builds: int
    settled_micro_usd: int
    reserved_micro_usd: int
    limit_micro_usd: int | None


class MemberUsageList(BaseModel):
    period_start: datetime
    period_end: datetime
    items: list[MemberUsage]
    next_cursor: None = None


class LimitIn(BaseModel):
    """A monthly limit in micro-USD, or null for none."""

    monthly_limit_micro_usd: Annotated[int, Field(ge=0, le=limits.MAX_LIMIT_MICRO_USD)] | None


class EntryOut(BaseModel):
    id: UUID
    build_id: UUID | None
    attempt_id: UUID | None
    kind: str
    amount_micro_usd: int
    created_at: datetime


class EntryList(BaseModel):
    items: list[EntryOut]
    next_cursor: str | None


class PresetOut(BaseModel):
    name: str
    description: str
    writer_model: str
    checker_model: str
    providers: list[str]
    data_regions: list[str]
    batch: bool = False  # the writer model has a catalog batch price (M6)
    own_key: bool = False  # the workspace given in the request can build it on its own key (M6)


class PresetList(BaseModel):
    price_version: str
    items: list[PresetOut]
    next_cursor: None = None


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


@router.get("/workspaces/{workspace_id}/usage", response_model=UsageOut)
async def usage(request: Request, db: Db, member: ReadMember) -> UsageOut:
    account = await ledger.current_account(
        db, member.workspace_id, _settings(request).generation_allowance_micro_usd, lock=False
    )
    room = await limits.room(db, member.workspace_id, account.id, member.user_id)
    used = (
        room.used_micro_usd
        if room
        else await limits.used(db, member.workspace_id, account.id, member.user_id)
    )
    await db.commit()
    return UsageOut(
        period_start=account.period_start,
        period_end=account.period_end,
        allowance_micro_usd=account.allowance_micro_usd,
        reserved_micro_usd=account.reserved_micro_usd,
        settled_micro_usd=account.settled_micro_usd,
        available_micro_usd=account.available_micro_usd,
        member_limit_micro_usd=room.limit_micro_usd if room else None,
        member_used_micro_usd=used,
    )


@router.get("/workspaces/{workspace_id}/usage/members", response_model=MemberUsageList)
async def usage_by_member(request: Request, db: Db, member: ReadMember) -> MemberUsageList:
    """This period's builds and spending of each member, and their limits (admins and owners)."""
    require_role(member, "admin")
    account = await ledger.current_account(
        db, member.workspace_id, _settings(request).generation_allowance_micro_usd, lock=False
    )
    rows = await limits.by_member(db, member.workspace_id, account.id)
    await db.commit()
    return MemberUsageList(
        period_start=account.period_start,
        period_end=account.period_end,
        items=[MemberUsage(**r) for r in rows],
    )


@router.put("/workspaces/{workspace_id}/members/{user_id}/spending-limit", status_code=204)
async def set_spending_limit(user_id: UUID, body: LimitIn, db: Db, member: WriteMember) -> Response:
    """Owners limit anyone; admins limit editors and viewers. Applies to builds started from now on."""
    require_role(member, "admin")
    await limits.set_limit(db, member, user_id, body.monthly_limit_micro_usd)
    await db.commit()
    return Response(status_code=204)


@router.get("/workspaces/{workspace_id}/usage/entries", response_model=EntryList)
async def usage_entries(
    request: Request,
    db: Db,
    member: ReadMember,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
) -> EntryList:
    require_role(member, "admin")
    secret = _settings(request).session_secret.get_secret_value()
    scope = f"usage-entries:{member.workspace_id}"
    position = decode_cursor(secret, scope, cursor)
    n = clamp_limit(limit)
    rows = list(
        await db.execute(
            text(
                "SELECT id, build_id, attempt_id, kind, amount_micro_usd, created_at FROM usage_entries"
                " WHERE workspace_id = :w AND (CAST(:t AS timestamptz) IS NULL OR (created_at, id) < (CAST(:t AS timestamptz), CAST(:i AS uuid)))"
                " ORDER BY created_at DESC, id DESC LIMIT :n"
            ),
            {
                "w": member.workspace_id,
                "t": position.created_at if position else None,
                "i": position.id if position else None,
                "n": n + 1,
            },
        )
    )
    await db.commit()
    more = len(rows) > n
    rows = rows[:n]
    return EntryList(
        items=[EntryOut(**dict(r._mapping)) for r in rows],
        next_cursor=encode_cursor(secret, scope, Position(rows[-1].created_at, rows[-1].id))
        if more
        else None,
    )


@router.get("/catalog/presets", response_model=PresetList)
async def presets(
    request: Request,
    db: Db,
    principal: CurrentPrincipal,
    workspace_id: Annotated[UUID | None, Query()] = None,
) -> PresetList:
    """Signed-in users only; the catalog holds no tenant data. With `workspace_id` of a workspace the
    caller belongs to, presets its own provider key can run are included too (M6, T24)."""
    catalog: Catalog = request.app.state.catalog
    items = []
    settings = _settings(request)
    own: set[str] = set()
    if workspace_id is not None:
        member = await read_member(workspace_id, db, principal)  # 404 for non-members
        own = await provider_keys.active_providers(db, member.workspace_id)
        await db.commit()
    for preset in catalog.presets:
        if not preset_available(catalog, preset.name, settings, own):
            continue
        models = [catalog.model(preset.writer.model), catalog.model(preset.checker.model)]
        items.append(
            PresetOut(
                name=preset.name,
                description=preset.description,
                writer_model=preset.writer.model,
                checker_model=preset.checker.model,
                providers=sorted({m.provider for m in models}),
                data_regions=sorted({m.data_region for m in models}),
                batch=batch_available(catalog, preset.name),
                own_key=own_key_usable(catalog, preset.name, own),
            )
        )
    return PresetList(price_version=catalog.revision, items=items)
