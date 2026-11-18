"""Usage summary, ledger and the approved model presets (06 "/workspaces/{wid}/usage", 11)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from sqlalchemy import text

from app.core.pagination import Position, clamp_limit, decode_cursor, encode_cursor
from app.core.settings import Settings
from app.engine.catalog import Catalog
from app.modules.identity.sessions import CurrentPrincipal, Db
from app.modules.projects.estimates import preset_available
from app.modules.usage import ledger
from app.modules.workspaces.access import ReadMember, require_role

router = APIRouter(tags=["usage"])


class UsageOut(BaseModel):
    period_start: datetime
    period_end: datetime
    allowance_micro_usd: int
    reserved_micro_usd: int
    settled_micro_usd: int
    available_micro_usd: int


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
    await db.commit()
    return UsageOut(
        period_start=account.period_start,
        period_end=account.period_end,
        allowance_micro_usd=account.allowance_micro_usd,
        reserved_micro_usd=account.reserved_micro_usd,
        settled_micro_usd=account.settled_micro_usd,
        available_micro_usd=account.available_micro_usd,
    )


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
async def presets(request: Request, principal: CurrentPrincipal) -> PresetList:
    del principal  # signed-in users only; the catalog holds no tenant data
    catalog: Catalog = request.app.state.catalog
    items = []
    settings = _settings(request)
    for preset in catalog.presets:
        if not preset_available(catalog, preset.name, settings):
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
            )
        )
    return PresetList(price_version=catalog.revision, items=items)
