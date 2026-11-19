"""Workspace provider keys (06 "/workspaces/{wid}/provider-keys", M6 T24). Owners manage; admins see
the masked hint. The key itself is never returned, logged or audited."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from app.core import ratelimit
from app.modules.identity.sessions import Db
from app.modules.providers import keys
from app.modules.workspaces.access import ReadMember, WriteMember, require_role

router = APIRouter(tags=["provider keys"])


class KeyIn(BaseModel):
    api_key: Annotated[str, Field(min_length=20, max_length=512, repr=False)]


class KeyOut(BaseModel):
    provider: str
    hint: str
    created_at: datetime
    verified_at: datetime | None
    created_by_name: str | None


class KeyList(BaseModel):
    items: list[KeyOut]
    next_cursor: None = None


@router.get("/workspaces/{workspace_id}/provider-keys", response_model=KeyList)
async def list_keys(db: Db, member: ReadMember) -> KeyList:
    require_role(member, "admin")
    return KeyList(items=[KeyOut(**k) for k in await keys.list_keys(db, member)])


@router.put("/workspaces/{workspace_id}/provider-keys/{provider}", response_model=KeyOut)
async def save_key(
    provider: Literal["anthropic"], body: KeyIn, request: Request, db: Db, member: WriteMember
) -> KeyOut:
    require_role(member, "owner")
    await ratelimit.hit(
        request.app.state.rate_sessions, request.app.state.settings, "billing", str(member.workspace_id)
    )
    saved = await keys.save_key(
        db, request.app.state.sealer, request.app.state.key_verifier, member, provider, body.api_key
    )
    return KeyOut(**saved)


@router.delete("/workspaces/{workspace_id}/provider-keys/{provider}", status_code=204)
async def remove_key(provider: Literal["anthropic"], db: Db, member: WriteMember) -> Response:
    require_role(member, "owner")
    await keys.remove_key(db, member, provider)
    return Response(status_code=204)
