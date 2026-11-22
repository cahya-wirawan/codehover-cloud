"""Handles and organizations (06 "/organizations", "/me/handle", 0038)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Response
from pydantic import BaseModel, Field

from app.modules.identity.sessions import CsrfPrincipal, CurrentPrincipal, Db
from app.modules.organizations import service
from app.modules.organizations.service import OrgRole

router = APIRouter(tags=["organizations"])

Handle = Annotated[str, Field(min_length=1, max_length=39)]


class HandleIn(BaseModel):
    handle: Handle


class HandleOut(BaseModel):
    handle: str


class HandleCheck(BaseModel):
    handle: str
    available: bool
    reason: str | None  # invalid, taken or reserved


@router.put("/me/handle", response_model=HandleOut)
async def set_my_handle(body: HandleIn, db: Db, principal: CsrfPrincipal) -> HandleOut:
    """Confirm the suggested handle (send it unchanged) or choose another; the old one stays reserved
    for this person for 90 days."""
    namespace = await service.own_namespace(db, principal.user_id)
    renamed = await service.rename(db, principal, namespace, body.handle.strip().lower())
    await db.commit()
    return HandleOut(handle=renamed)


@router.get("/handles/{handle}", response_model=HandleCheck)
async def check_handle(
    handle: Annotated[str, Path(max_length=39)], db: Db, principal: CurrentPrincipal
) -> HandleCheck:
    mine = await service.own_namespace(db, principal.user_id)
    reason = await service.handle_unavailable(db, handle, mine)
    await db.commit()
    return HandleCheck(handle=handle, available=reason is None, reason=reason)


# ---------------------------------------------------------------- organizations
class OrganizationIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    handle: Handle


class OrganizationRename(BaseModel):
    name: str = Field(min_length=2, max_length=80)


class OrganizationSummary(BaseModel):
    id: UUID
    name: str
    handle: str
    role: OrgRole


class InvitationOrganization(BaseModel):
    id: UUID
    name: str
    handle: str


class MyInvitation(BaseModel):
    id: UUID
    role: OrgRole
    expires_at: datetime
    organization: InvitationOrganization


class OrganizationList(BaseModel):
    items: list[OrganizationSummary]
    invitations: list[MyInvitation]
    next_cursor: None = None


class OrganizationOut(OrganizationSummary):
    created_at: datetime


class OrgWorkspace(BaseModel):
    id: UUID
    name: str
    slug: str
    role: str


class OrgWorkspaceList(BaseModel):
    items: list[OrgWorkspace]
    next_cursor: None = None


class OrgMemberOut(BaseModel):
    user_id: UUID
    display_name: str
    handle: str
    role: OrgRole
    created_at: datetime


class OrgMemberList(BaseModel):
    items: list[OrgMemberOut]
    next_cursor: None = None


class RoleIn(BaseModel):
    role: OrgRole


class InvitationIn(BaseModel):
    handle: Handle
    role: OrgRole = "member"


class OrgInvitationOut(BaseModel):
    id: UUID
    handle: str
    role: OrgRole
    created_at: datetime
    expires_at: datetime


class OrgInvitationList(BaseModel):
    items: list[OrgInvitationOut]
    next_cursor: None = None


@router.get("/organizations", response_model=OrganizationList)
async def list_organizations(db: Db, principal: CurrentPrincipal) -> OrganizationList:
    return OrganizationList(**await service.listing(db, principal))


@router.post("/organizations", response_model=OrganizationOut, status_code=201)
async def create_organization(body: OrganizationIn, db: Db, principal: CsrfPrincipal) -> OrganizationOut:
    organization_id = await service.create(db, principal, body.name, body.handle.strip().lower())
    found = await service.member(db, principal, organization_id)
    out = OrganizationOut(**await service.get(db, found))
    await db.commit()
    return out


@router.get("/organizations/{organization_id}", response_model=OrganizationOut)
async def get_organization(organization_id: UUID, db: Db, principal: CurrentPrincipal) -> OrganizationOut:
    found = await service.member(db, principal, organization_id)
    out = OrganizationOut(**await service.get(db, found))
    await db.commit()
    return out


@router.patch("/organizations/{organization_id}", response_model=OrganizationOut)
async def rename_organization(
    organization_id: UUID, body: OrganizationRename, db: Db, principal: CsrfPrincipal
) -> OrganizationOut:
    found = await service.member(db, principal, organization_id, "admin")
    await service.rename_organization(db, found, body.name)
    found = await service.member(db, principal, organization_id)
    out = OrganizationOut(**await service.get(db, found))
    await db.commit()
    return out


@router.put("/organizations/{organization_id}/handle", response_model=HandleOut)
async def rename_handle(organization_id: UUID, body: HandleIn, db: Db, principal: CsrfPrincipal) -> HandleOut:
    found = await service.member(db, principal, organization_id, "owner")
    namespace = (await service.get(db, found))["namespace_id"]
    renamed = await service.rename(db, principal, namespace, body.handle.strip().lower())
    await db.commit()
    return HandleOut(handle=renamed)


@router.delete("/organizations/{organization_id}", status_code=204)
async def delete_organization(organization_id: UUID, db: Db, principal: CsrfPrincipal) -> Response:
    found = await service.member(db, principal, organization_id, "owner")
    await service.delete(db, found)
    return Response(status_code=204)


@router.get("/organizations/{organization_id}/workspaces", response_model=OrgWorkspaceList)
async def organization_workspaces(
    organization_id: UUID, db: Db, principal: CurrentPrincipal
) -> OrgWorkspaceList:
    found = await service.member(db, principal, organization_id)
    items = await service.workspaces(db, found)
    await db.commit()
    return OrgWorkspaceList(items=[OrgWorkspace(**w) for w in items])


@router.get("/organizations/{organization_id}/members", response_model=OrgMemberList)
async def organization_members(organization_id: UUID, db: Db, principal: CurrentPrincipal) -> OrgMemberList:
    found = await service.member(db, principal, organization_id)
    items = await service.members(db, found)
    await db.commit()
    return OrgMemberList(items=[OrgMemberOut(**m) for m in items])


@router.patch("/organizations/{organization_id}/members/{user_id}", status_code=204)
async def change_member_role(
    organization_id: UUID, user_id: UUID, body: RoleIn, db: Db, principal: CsrfPrincipal
) -> Response:
    found = await service.member(db, principal, organization_id)
    await service.change_role(db, found, user_id, body.role)
    return Response(status_code=204)


@router.delete("/organizations/{organization_id}/members/{user_id}", status_code=204)
async def remove_member(organization_id: UUID, user_id: UUID, db: Db, principal: CsrfPrincipal) -> Response:
    found = await service.member(db, principal, organization_id)
    await service.remove_member(db, found, user_id)
    return Response(status_code=204)


@router.get("/organizations/{organization_id}/invitations", response_model=OrgInvitationList)
async def organization_invitations(
    organization_id: UUID, db: Db, principal: CurrentPrincipal
) -> OrgInvitationList:
    found = await service.member(db, principal, organization_id, "admin")
    items = await service.invitations(db, found)
    await db.commit()
    return OrgInvitationList(items=[OrgInvitationOut(**i) for i in items])


@router.post("/organizations/{organization_id}/invitations", response_model=OrgInvitationOut, status_code=201)
async def invite(
    organization_id: UUID, body: InvitationIn, db: Db, principal: CsrfPrincipal
) -> OrgInvitationOut:
    found = await service.member(db, principal, organization_id, "admin")
    return OrgInvitationOut(**await service.invite(db, found, body.handle, body.role))


@router.delete("/organizations/{organization_id}/invitations/{invitation_id}", status_code=204)
async def revoke_invitation(
    organization_id: UUID, invitation_id: UUID, db: Db, principal: CsrfPrincipal
) -> Response:
    found = await service.member(db, principal, organization_id, "admin")
    await service.revoke_invitation(db, found, invitation_id)
    return Response(status_code=204)


class Answered(BaseModel):
    organization_id: UUID


@router.post("/organization-invitations/{invitation_id}/accept", response_model=Answered)
async def accept(invitation_id: UUID, db: Db, principal: CsrfPrincipal) -> Answered:
    return Answered(organization_id=await service.answer(db, principal, invitation_id, True))


@router.post("/organization-invitations/{invitation_id}/decline", response_model=Answered)
async def decline(invitation_id: UUID, db: Db, principal: CsrfPrincipal) -> Answered:
    return Answered(organization_id=await service.answer(db, principal, invitation_id, False))
