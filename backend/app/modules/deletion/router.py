"""Deletion and account export endpoints (06 "/projects/{pid}/deletion", "/me/export", "/me/deletion")."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from app.adapters.storage import ObjectStore
from app.modules.deletion import service
from app.modules.identity.sessions import CsrfPrincipal, Db, clear_session_cookie
from app.modules.workspaces.access import Member, WriteMember, resource_member

router = APIRouter(tags=["deletion"])


async def project_admin(project_id: UUID, db: Db, principal: CsrfPrincipal) -> Member:
    return await resource_member("project", project_id, db, principal)


ProjectAdmin = Annotated[Member, Depends(project_admin)]


class Confirmation(BaseModel):
    confirmation: Annotated[str, Field(max_length=200)]


class AccountDeletionIn(Confirmation):
    delete_workspaces: Annotated[list[UUID], Field(max_length=100)] = []


class DeletionOut(BaseModel):
    id: UUID
    scope: Literal["project", "workspace", "account"]
    state: Literal["pending"] = "pending"
    requested_at: datetime
    purge_after: datetime


class AccountDeletionOut(BaseModel):
    account: DeletionOut
    workspaces: list[DeletionOut]


def _out(t: service.Tombstone) -> DeletionOut:
    return DeletionOut(id=t.id, scope=t.scope, requested_at=t.requested_at, purge_after=t.not_before)


def _store(request: Request) -> ObjectStore:
    store: ObjectStore = request.app.state.objects
    return store


@router.post("/projects/{project_id}/deletion", response_model=DeletionOut, status_code=202)
async def delete_project(
    project_id: UUID, body: Confirmation, request: Request, db: Db, member: ProjectAdmin
) -> DeletionOut:
    tombstone = await service.delete_project(db, member, project_id, body.confirmation)
    await service.record_tombstone(_store(request), tombstone)
    return _out(tombstone)


@router.post("/workspaces/{workspace_id}/deletion", response_model=DeletionOut, status_code=202)
async def delete_workspace(body: Confirmation, request: Request, db: Db, member: WriteMember) -> DeletionOut:
    tombstone = await service.delete_workspace(db, member, body.confirmation)
    await service.record_tombstone(_store(request), tombstone)
    return _out(tombstone)


@router.post("/me/deletion", response_model=AccountDeletionOut, status_code=202)
async def delete_account(
    body: AccountDeletionIn, request: Request, response: Response, db: Db, principal: CsrfPrincipal
) -> AccountDeletionOut:
    deleted = await service.delete_account(db, principal, body.confirmation, body.delete_workspaces)
    for tombstone in (deleted.account, *deleted.workspaces):
        await service.record_tombstone(_store(request), tombstone)
    clear_session_cookie(response, request.app.state.settings)
    return AccountDeletionOut(account=_out(deleted.account), workspaces=[_out(t) for t in deleted.workspaces])


@router.post(
    "/me/export",
    responses={200: {"content": {"application/json": {}}, "description": "The account data as a download"}},
)
async def export_account(db: Db, principal: CsrfPrincipal) -> Response:
    document: dict[str, Any] = await service.export_account(db, principal)
    return Response(
        json.dumps(document, ensure_ascii=False, indent=1),
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="codehover-account.json"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
