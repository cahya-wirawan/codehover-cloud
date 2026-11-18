"""Workspace, member and invitation endpoints (06 "Endpoint inventory")."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.pagination import Position, clamp_limit, decode_cursor, encode_cursor
from app.core.settings import Settings
from app.modules.identity.sessions import CsrfPrincipal, CurrentPrincipal, Db
from app.modules.workspaces import service
from app.modules.workspaces.access import ReadMember, Role, WriteMember, require_role

router = APIRouter(tags=["workspaces"])

SLUG = r"^[a-z0-9][a-z0-9-]{1,46}[a-z0-9]$"


# ---------------------------------------------------------------- models
class WorkspaceCreate(BaseModel):
    name: Annotated[str, Field(min_length=2, max_length=80)]
    slug: Annotated[str, Field(pattern=SLUG)]


class WorkspaceRename(BaseModel):
    name: Annotated[str, Field(min_length=2, max_length=80)]


class WorkspaceSummary(BaseModel):
    id: UUID
    name: str
    slug: str
    role: Role


class WorkspaceList(BaseModel):
    items: list[WorkspaceSummary]
    next_cursor: None = None


class MemberOut(BaseModel):
    user_id: UUID
    display_name: str
    role: Role
    created_at: datetime


class MemberList(BaseModel):
    items: list[MemberOut]
    next_cursor: str | None


class RoleChange(BaseModel):
    role: Role


class InvitationCreate(BaseModel):
    email: Annotated[str, Field(min_length=3, max_length=254)]
    role: Role


class InvitationOut(BaseModel):
    id: UUID
    email: str
    role: Role
    expires_at: datetime


class InvitationCreated(InvitationOut):
    accept_url: str  # shown once; the token is in the fragment so it never reaches server logs


class InvitationList(BaseModel):
    items: list[InvitationOut]
    next_cursor: None = None


class AcceptInvitation(BaseModel):
    token: Annotated[str, Field(min_length=16, max_length=256)]


class Accepted(BaseModel):
    workspace_id: UUID


# ---------------------------------------------------------------- workspaces
def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


@router.get("/workspaces", response_model=WorkspaceList)
async def list_workspaces(db: Db, principal: CurrentPrincipal) -> WorkspaceList:
    items = await service.list_workspaces(db, principal)
    return WorkspaceList(items=[WorkspaceSummary(**w) for w in items])


@router.post("/workspaces", response_model=WorkspaceSummary, status_code=201)
async def create_workspace(body: WorkspaceCreate, db: Db, principal: CsrfPrincipal) -> WorkspaceSummary:
    workspace_id = await service.create_workspace(db, principal, body.name.strip(), body.slug)
    return WorkspaceSummary(id=workspace_id, name=body.name.strip(), slug=body.slug, role="owner")


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceSummary)
async def get_workspace(db: Db, member: ReadMember, response: Response) -> WorkspaceSummary:
    workspace = await service.get_workspace(db, member)
    response.headers["ETag"] = service.etag(workspace["updated_at"])
    return WorkspaceSummary(**workspace)


@router.patch("/workspaces/{workspace_id}", response_model=WorkspaceSummary)
async def rename_workspace(
    body: WorkspaceRename,
    db: Db,
    member: WriteMember,
    response: Response,
    if_match: Annotated[str | None, Header()] = None,
) -> WorkspaceSummary:
    require_role(member, "admin")
    workspace = await service.rename_workspace(db, member, body.name.strip(), if_match)
    response.headers["ETag"] = service.etag(workspace["updated_at"])
    return WorkspaceSummary(**workspace)


# ---------------------------------------------------------------- members
@router.get("/workspaces/{workspace_id}/members", response_model=MemberList)
async def list_members(
    request: Request,
    db: Db,
    member: ReadMember,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
) -> MemberList:
    secret = _settings(request).session_secret.get_secret_value()
    scope = f"members:{member.workspace_id}"
    items, last = await service.list_members(
        db, member, decode_cursor(secret, scope, cursor), clamp_limit(limit)
    )
    return MemberList(
        items=[MemberOut(**m) for m in items],
        next_cursor=encode_cursor(secret, scope, last) if last else None,
    )


@router.patch("/workspaces/{workspace_id}/members/{user_id}", status_code=204)
async def change_role(user_id: UUID, body: RoleChange, db: Db, member: WriteMember) -> Response:
    require_role(member, "admin")
    await service.change_role(db, member, user_id, body.role)
    return Response(status_code=204)


@router.delete("/workspaces/{workspace_id}/members/{user_id}", status_code=204)
async def remove_member(user_id: UUID, db: Db, member: WriteMember) -> Response:
    if user_id != member.user_id:
        require_role(member, "admin")
    await service.remove_member(db, member, user_id)
    return Response(status_code=204)


# ---------------------------------------------------------------- invitations
@router.get("/workspaces/{workspace_id}/invitations", response_model=InvitationList)
async def list_invitations(db: Db, member: ReadMember) -> InvitationList:
    require_role(member, "admin")
    return InvitationList(items=[InvitationOut(**i) for i in await service.list_invitations(db, member)])


@router.post("/workspaces/{workspace_id}/invitations", response_model=InvitationCreated, status_code=201)
async def create_invitation(
    body: InvitationCreate, request: Request, db: Db, member: WriteMember
) -> InvitationCreated:
    require_role(member, "admin")
    created = await service.invite(db, member, body.email, body.role)
    return InvitationCreated(
        id=created.id,
        email=created.email,
        role=created.role,
        expires_at=created.expires_at,
        accept_url=f"{_settings(request).public_origin}/invitations/accept#token={created.token}",
    )


@router.delete("/workspaces/{workspace_id}/invitations/{invitation_id}", status_code=204)
async def revoke_invitation(invitation_id: UUID, db: Db, member: WriteMember) -> Response:
    require_role(member, "admin")
    await service.revoke_invitation(db, member, invitation_id)
    return Response(status_code=204)


@router.post("/invitations/accept", response_model=Accepted)
async def accept_invitation(body: AcceptInvitation, db: Db, principal: CsrfPrincipal) -> Accepted:
    return Accepted(workspace_id=await service.accept_invitation(db, principal, body.token))


# ---------------------------------------------------------------- audit (FR-023)
class AuditEntry(BaseModel):
    id: UUID
    action: str
    actor_user_id: UUID | None
    actor_name: str | None
    resource_type: str
    resource_id: UUID | None
    metadata: dict[str, Any]
    request_id: str | None
    created_at: datetime


class AuditList(BaseModel):
    items: list[AuditEntry]
    next_cursor: str | None


@router.get("/workspaces/{workspace_id}/audit", response_model=AuditList)
async def audit_log(
    request: Request,
    db: Db,
    member: ReadMember,
    action: Annotated[str | None, Query(pattern=r"^[a-z_]+(\.[a-z_]+)*\.?$", max_length=64)] = None,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
) -> AuditList:
    """Workspace audit events, newest first; an action prefix like `publication.` filters (09: admins)."""
    require_role(member, "admin")
    secret = _settings(request).session_secret.get_secret_value()
    scope = f"audit:{member.workspace_id}:{action or ''}"
    position = decode_cursor(secret, scope, cursor)
    n = clamp_limit(limit)
    prefix = (action or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = list(
        await db.execute(
            text(
                "SELECT a.id, a.action, a.actor_user_id, u.display_name AS actor_name, a.resource_type, a.resource_id,"
                " a.metadata, a.request_id, a.created_at FROM audit_events a LEFT JOIN users u ON u.id = a.actor_user_id"
                " WHERE a.workspace_id = :w AND (CAST(:prefix AS text) = '' OR a.action LIKE :like ESCAPE '\\')"
                " AND (CAST(:t AS timestamptz) IS NULL OR (a.created_at, a.id) < (CAST(:t AS timestamptz), CAST(:i AS uuid)))"
                " ORDER BY a.created_at DESC, a.id DESC LIMIT :n"
            ),
            {
                "w": member.workspace_id,
                "prefix": prefix,
                "like": prefix + ("%" if action and action.endswith(".") else ""),
                "t": position.created_at if position else None,
                "i": position.id if position else None,
                "n": n + 1,
            },
        )
    )
    await db.commit()
    more = len(rows) > n
    rows = rows[:n]
    return AuditList(
        items=[AuditEntry(**dict(r._mapping)) for r in rows],
        next_cursor=encode_cursor(secret, scope, Position(rows[-1].created_at, rows[-1].id))
        if more
        else None,
    )
