"""Handles and organizations (0038; 01, 03, 06, 09).

Every person has a handle in one global namespace with organizations; an organization's owners and admins
are owners and admins of every workspace it holds, and plain members see only the workspaces they join.
"""

from __future__ import annotations

import secrets
from typing import Any

import httpx
import pytest

from tests.integration.auth_support import ddl, unsafe
from tests.integration.conftest import needs_db
from tests.integration.test_workspaces import NewUser, code, invite, join, me, new_user  # noqa: F401

pytestmark = [pytest.mark.integration, needs_db]


def handle() -> str:
    return f"h{secrets.token_hex(4)}"


async def set_handle(person: httpx.AsyncClient, value: str) -> httpx.Response:
    return await unsafe(person, "PUT", "/api/v1/me/handle", json={"handle": value})


async def create_org(
    owner: httpx.AsyncClient, name: str = "Acme", value: str | None = None
) -> dict[str, Any]:
    response = await unsafe(
        owner, "POST", "/api/v1/organizations", json={"name": name, "handle": value or handle()}
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def org_workspace(admin: httpx.AsyncClient, org: str) -> httpx.Response:
    return await unsafe(
        admin,
        "POST",
        "/api/v1/workspaces",
        json={"name": "Docs", "slug": f"ws-{secrets.token_hex(4)}", "organization_id": org},
    )


async def add_member(owner: httpx.AsyncClient, org: str, person: httpx.AsyncClient, role: str) -> None:
    their = (await person.get("/api/v1/me")).json()["handle"]
    invited = await unsafe(
        owner, "POST", f"/api/v1/organizations/{org}/invitations", json={"handle": their, "role": role}
    )
    assert invited.status_code == 201, invited.text
    accepted = await unsafe(person, "POST", f"/api/v1/organization-invitations/{invited.json()['id']}/accept")
    assert accepted.status_code == 200, accepted.text


async def test_a_person_gets_a_suggested_handle_and_confirms_or_changes_it(new_user: NewUser) -> None:
    person = await new_user()
    profile = (await person.get("/api/v1/me")).json()
    local = person.email.split("@")[0]  # type: ignore[attr-defined]
    assert profile["handle"] == local and profile["handle_confirmed"] is False  # suggested from the email
    confirmed = await set_handle(person, profile["handle"])
    assert (
        confirmed.status_code == 200 and (await person.get("/api/v1/me")).json()["handle_confirmed"] is True
    )
    for bad in ("b_d", "-x", "x-", "a--b", "api", "sites", "x" * 40):
        refused = await set_handle(person, bad)
        assert refused.status_code == 422, bad
    other = await new_user()
    taken = await set_handle(other, profile["handle"])
    assert taken.status_code == 409 and code(taken) == "HANDLE_TAKEN"
    new = handle()
    assert (await set_handle(person, new)).json() == {"handle": new}
    # The old handle stays reserved for this person: nobody else may take it, they may take it back.
    reserved = await set_handle(other, profile["handle"])
    assert reserved.status_code == 409 and code(reserved) == "HANDLE_TAKEN"
    check = (await other.get(f"/api/v1/handles/{profile['handle']}")).json()
    assert check == {"handle": profile["handle"], "available": False, "reason": "reserved"}
    assert (await person.get(f"/api/v1/handles/{profile['handle']}")).json()["available"] is True
    assert (await set_handle(person, profile["handle"])).status_code == 200


async def test_organization_owners_and_admins_manage_all_its_workspaces(new_user: NewUser) -> None:
    owner, admin, member, outsider = await new_user(), await new_user(), await new_user(), await new_user()
    org = await create_org(owner)
    assert org["role"] == "owner"
    first = await org_workspace(owner, org["id"])
    assert first.status_code == 201, first.text
    ws = first.json()["id"]
    assert first.json()["owner"] == {
        "kind": "organization",
        "handle": org["handle"],
        "organization_id": org["id"],
    }
    await add_member(owner, org["id"], admin, "admin")
    await add_member(owner, org["id"], member, "member")
    roles = {
        m["handle"]: m["role"]
        for m in (await owner.get(f"/api/v1/organizations/{org['id']}/members")).json()["items"]
    }
    assert sorted(roles.values()) == ["admin", "member", "owner"]

    # The admin is an admin of the existing workspace, and of one created later; a plain member is not.
    assert (await admin.get(f"/api/v1/workspaces/{ws}")).json()["role"] == "admin"
    assert (await member.get(f"/api/v1/workspaces/{ws}")).status_code == 404
    second = await org_workspace(admin, org["id"])
    assert second.status_code == 201 and second.json()["role"] == "admin"
    assert (await owner.get(f"/api/v1/workspaces/{second.json()['id']}")).json()["role"] == "owner"
    assert (await org_workspace(member, org["id"])).status_code == 403
    assert (await org_workspace(outsider, org["id"])).status_code == 403
    listed = {
        w["id"] for w in (await member.get(f"/api/v1/organizations/{org['id']}/workspaces")).json()["items"]
    }
    assert listed == set()

    # A plain member added to a workspace keeps that role; the organization's roles are managed there.
    await join(owner, ws, member, "viewer")
    assert (await member.get(f"/api/v1/workspaces/{ws}")).json()["role"] == "viewer"
    admin_id = await me(admin)
    managed = await unsafe(
        owner, "PATCH", f"/api/v1/workspaces/{ws}/members/{admin_id}", json={"role": "viewer"}
    )
    assert managed.status_code == 409 and code(managed) == "MANAGED_BY_ORGANIZATION"
    members = (await owner.get(f"/api/v1/workspaces/{ws}/members")).json()["items"]
    assert {m["user_id"]: m["via_organization"] for m in members}[admin_id] is True

    # Promoting the viewer to organization admin raises them to admin; demoting them restores viewer.
    member_id = await me(member)
    assert (
        await unsafe(
            owner, "PATCH", f"/api/v1/organizations/{org['id']}/members/{member_id}", json={"role": "admin"}
        )
    ).status_code == 204
    assert (await member.get(f"/api/v1/workspaces/{ws}")).json()["role"] == "admin"
    assert (
        await unsafe(
            owner, "PATCH", f"/api/v1/organizations/{org['id']}/members/{member_id}", json={"role": "member"}
        )
    ).status_code == 204
    assert (await member.get(f"/api/v1/workspaces/{ws}")).json()["role"] == "viewer"

    # Removing the admin from the organization removes them from its workspaces, tokens included.
    token = await unsafe(
        admin,
        "POST",
        f"/api/v1/workspaces/{ws}/access-tokens",
        json={"name": "t", "expires_in_days": 30},
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )
    assert token.status_code == 201, token.text
    removed = await unsafe(owner, "DELETE", f"/api/v1/organizations/{org['id']}/members/{admin_id}")
    assert removed.status_code == 204
    assert (await admin.get(f"/api/v1/workspaces/{ws}")).status_code == 404
    assert ddl("SELECT revoked_at IS NOT NULL FROM access_tokens WHERE id = :t", t=token.json()["id"])[0][0]

    # Roles: only owners change them; the last owner stays.
    owner_id = await me(owner)
    last = await unsafe(
        owner, "PATCH", f"/api/v1/organizations/{org['id']}/members/{owner_id}", json={"role": "admin"}
    )
    assert last.status_code == 409 and code(last) == "LAST_OWNER"
    assert (
        await unsafe(
            member, "PATCH", f"/api/v1/organizations/{org['id']}/members/{owner_id}", json={"role": "member"}
        )
    ).status_code == 403
    assert (await outsider.get(f"/api/v1/organizations/{org['id']}")).status_code == 404


async def test_invitations_are_by_handle_and_answered_by_the_person(new_user: NewUser) -> None:
    owner, person, admin = await new_user(), await new_user(), await new_user()
    org = await create_org(owner)
    await add_member(owner, org["id"], admin, "admin")
    their = (await person.get("/api/v1/me")).json()["handle"]
    assert (
        await unsafe(
            admin,
            "POST",
            f"/api/v1/organizations/{org['id']}/invitations",
            json={"handle": their, "role": "owner"},
        )
    ).status_code == 403  # admins invite plain members only
    invited = await unsafe(
        admin, "POST", f"/api/v1/organizations/{org['id']}/invitations", json={"handle": their}
    )
    assert invited.status_code == 201
    again = await unsafe(
        admin, "POST", f"/api/v1/organizations/{org['id']}/invitations", json={"handle": their}
    )
    assert again.status_code == 409 and code(again) == "ALREADY_INVITED"
    nobody = await unsafe(
        admin, "POST", f"/api/v1/organizations/{org['id']}/invitations", json={"handle": "no-one-x9"}
    )
    assert nobody.status_code == 404
    pending = (await person.get("/api/v1/organizations")).json()
    assert pending["items"] == [] and [i["organization"]["handle"] for i in pending["invitations"]] == [
        org["handle"]
    ]
    # Someone else cannot answer it.
    assert (
        await unsafe(admin, "POST", f"/api/v1/organization-invitations/{invited.json()['id']}/accept")
    ).status_code == 404
    declined = await unsafe(
        person, "POST", f"/api/v1/organization-invitations/{invited.json()['id']}/decline"
    )
    assert declined.status_code == 200
    assert (await person.get(f"/api/v1/organizations/{org['id']}")).status_code == 404
    second = await unsafe(
        owner, "POST", f"/api/v1/organizations/{org['id']}/invitations", json={"handle": their}
    )
    assert (
        await unsafe(person, "POST", f"/api/v1/organization-invitations/{second.json()['id']}/accept")
    ).status_code == 200
    assert (await person.get(f"/api/v1/organizations/{org['id']}")).json()["role"] == "member"
    assert (
        await unsafe(person, "DELETE", f"/api/v1/organizations/{org['id']}/members/{await me(person)}")
    ).status_code == 204


async def test_organization_handles_and_deletion(new_user: NewUser) -> None:
    owner, other = await new_user(), await new_user()
    first = handle()
    org = await create_org(owner, value=first)
    taken = await unsafe(other, "POST", "/api/v1/organizations", json={"name": "Copy", "handle": first})
    assert taken.status_code == 409 and code(taken) == "HANDLE_TAKEN"
    person_handle = (await other.get("/api/v1/me")).json()["handle"]
    clash = await unsafe(
        owner, "POST", "/api/v1/organizations", json={"name": "Clash", "handle": person_handle}
    )
    assert clash.status_code == 409  # one namespace for people and organizations
    renamed = handle()
    assert (
        await unsafe(owner, "PUT", f"/api/v1/organizations/{org['id']}/handle", json={"handle": renamed})
    ).json() == {"handle": renamed}
    assert (
        await unsafe(other, "POST", "/api/v1/organizations", json={"name": "Take", "handle": first})
    ).status_code == 409
    ws = (await org_workspace(owner, org["id"])).json()["id"]
    busy = await unsafe(owner, "DELETE", f"/api/v1/organizations/{org['id']}")
    assert busy.status_code == 409 and code(busy) == "ORGANIZATION_NOT_EMPTY"
    ddl("UPDATE workspaces SET deleted_at = now(), status = 'deleting' WHERE id = :w", w=ws)
    assert (await unsafe(owner, "DELETE", f"/api/v1/organizations/{org['id']}")).status_code == 204
    assert (await owner.get(f"/api/v1/organizations/{org['id']}")).status_code == 404
    # The deleted organization's handle is reserved for nobody for 90 days.
    gone = await unsafe(other, "POST", "/api/v1/organizations", json={"name": "Again", "handle": renamed})
    assert gone.status_code == 409
    assert ddl("SELECT namespace_id FROM handle_reservations WHERE handle = :h", h=renamed) == [(None,)]


async def test_account_deletion_needs_another_owner_for_organizations_owned_alone(new_user: NewUser) -> None:
    owner, partner = await new_user(), await new_user()
    org = await create_org(owner)
    await add_member(owner, org["id"], partner, "member")
    owner_handle = (await owner.get("/api/v1/me")).json()["handle"]
    ddl("UPDATE sessions SET reauthenticated_at = now() WHERE user_id = :u", u=await me(owner))
    body = {"confirmation": "delete my account", "delete_workspaces": []}
    refused = await unsafe(owner, "POST", "/api/v1/me/deletion", json=body)
    assert refused.status_code == 409, refused.text
    assert [o["handle"] for o in refused.json()["error"]["details"]["organizations"]] == [org["handle"]]
    partner_id = await me(partner)
    assert (
        await unsafe(
            owner, "PATCH", f"/api/v1/organizations/{org['id']}/members/{partner_id}", json={"role": "owner"}
        )
    ).status_code == 204
    deleted = await unsafe(owner, "POST", "/api/v1/me/deletion", json=body)
    assert deleted.status_code in (200, 202), deleted.text
    roles = {
        m["user_id"]
        for m in (await partner.get(f"/api/v1/organizations/{org['id']}/members")).json()["items"]
    }
    assert roles == {partner_id}
    assert ddl("SELECT namespace_id FROM handle_reservations WHERE handle = :h", h=owner_handle) == [(None,)]
