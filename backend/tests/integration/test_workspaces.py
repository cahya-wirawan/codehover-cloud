"""Workspaces, roles and invitations against PostgreSQL 18 (T04; 09 "Permission matrix", A04, A17)."""

from __future__ import annotations

import secrets
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import sqlalchemy as sa

from tests.integration.auth_support import FakeIdP, browser, ddl, sign_in, unsafe
from tests.integration.conftest import RUNTIME_URL, engine, needs_db

pytestmark = [pytest.mark.integration, needs_db]

NewUser = Callable[..., Awaitable[httpx.AsyncClient]]


@pytest.fixture
async def new_user(client: httpx.AsyncClient, idp: FakeIdP) -> AsyncIterator[NewUser]:
    """Sign in a fresh person in their own browser; email is verified unless None."""
    app = client.app  # type: ignore[attr-defined]
    async with AsyncExitStack() as stack:

        async def make(email: str | None = "auto") -> httpx.AsyncClient:
            person = await stack.enter_async_context(browser(app))
            idp.google_sub = secrets.token_hex(8)
            idp.google_email = f"{secrets.token_hex(4)}@example.test" if email == "auto" else email
            await sign_in(person, idp)
            person.email = idp.google_email  # type: ignore[attr-defined]
            return person

        yield make


async def create_workspace(owner: httpx.AsyncClient) -> str:
    slug = f"ws-{secrets.token_hex(4)}"
    response = await unsafe(owner, "POST", "/api/v1/workspaces", json={"name": "Team", "slug": slug})
    assert response.status_code == 201, response.text
    workspace_id: str = response.json()["id"]
    return workspace_id


async def me(person: httpx.AsyncClient) -> str:
    user_id: str = (await person.get("/api/v1/me")).json()["id"]
    return user_id


async def invite(admin: httpx.AsyncClient, ws: str, email: str, role: str) -> httpx.Response:
    return await unsafe(
        admin, "POST", f"/api/v1/workspaces/{ws}/invitations", json={"email": email, "role": role}
    )


def token_of(invitation: httpx.Response) -> str:
    return parse_qs(urlsplit(invitation.json()["accept_url"]).fragment)["token"][0]


async def accept(person: httpx.AsyncClient, token: str) -> httpx.Response:
    return await unsafe(person, "POST", "/api/v1/invitations/accept", json={"token": token})


async def join(owner: httpx.AsyncClient, ws: str, person: httpx.AsyncClient, role: str) -> None:
    created = await invite(owner, ws, person.email, role)  # type: ignore[attr-defined]
    assert created.status_code == 201, created.text
    assert (await accept(person, token_of(created))).status_code == 200


def code(response: httpx.Response) -> str:
    error_code: str = response.json()["error"]["code"]
    return error_code


# ---------------------------------------------------------------- workspaces
async def test_create_list_and_slug_conflict(new_user: NewUser) -> None:
    owner = await new_user()
    ws = await create_workspace(owner)
    listed = (await owner.get("/api/v1/workspaces")).json()["items"]
    assert [(w["id"], w["role"]) for w in listed] == [(ws, "owner")]
    slug = next(w["slug"] for w in listed)
    duplicate = await unsafe(owner, "POST", "/api/v1/workspaces", json={"name": "Other", "slug": slug})
    assert duplicate.status_code == 409 and code(duplicate) == "SLUG_TAKEN"
    bad = await unsafe(owner, "POST", "/api/v1/workspaces", json={"name": "Bad", "slug": "Not_A_Slug"})
    assert bad.status_code == 422


async def test_a_workspace_needs_only_a_name(new_user: NewUser) -> None:
    # The slug is internal (links use the ID, sites the owner and project): made from the name, with a
    # suffix when the name's is taken, by anyone.
    first, second = await new_user(), await new_user()
    name = f"Docs Team {secrets.token_hex(3)}"
    one = await unsafe(first, "POST", "/api/v1/workspaces", json={"name": name})
    assert one.status_code == 201, one.text
    assert one.json()["name"] == name and one.json()["slug"] == name.lower().replace(" ", "-")
    two = await unsafe(second, "POST", "/api/v1/workspaces", json={"name": name})
    assert two.status_code == 201, two.text
    assert (
        two.json()["slug"].startswith(one.json()["slug"] + "-") and two.json()["slug"] != one.json()["slug"]
    )
    short = await unsafe(first, "POST", "/api/v1/workspaces", json={"name": "Ü!"})
    assert short.status_code == 201 and short.json()["slug"].startswith("workspace")


async def test_non_members_get_404_everywhere(new_user: NewUser) -> None:
    owner, stranger = await new_user(), await new_user()
    ws = await create_workspace(owner)
    for path in ("", "/members", "/invitations"):
        assert (await stranger.get(f"/api/v1/workspaces/{ws}{path}")).status_code == 404
    rename = await unsafe(stranger, "PATCH", f"/api/v1/workspaces/{ws}", json={"name": "Mine"})
    assert rename.status_code == 404
    assert (await stranger.get("/api/v1/workspaces")).json()["items"] == []


async def test_rename_requires_admin_and_current_etag(new_user: NewUser) -> None:
    owner, viewer = await new_user(), await new_user()
    ws = await create_workspace(owner)
    await join(owner, ws, viewer, "viewer")
    current = await owner.get(f"/api/v1/workspaces/{ws}")
    tag = current.headers["etag"]
    forbidden = await unsafe(
        viewer, "PATCH", f"/api/v1/workspaces/{ws}", json={"name": "Hijack"}, headers={"If-Match": tag}
    )
    assert forbidden.status_code == 403
    missing = await unsafe(owner, "PATCH", f"/api/v1/workspaces/{ws}", json={"name": "Renamed"})
    assert missing.status_code == 428
    ok = await unsafe(
        owner, "PATCH", f"/api/v1/workspaces/{ws}", json={"name": "Renamed"}, headers={"If-Match": tag}
    )
    assert ok.status_code == 200 and ok.json()["name"] == "Renamed" and ok.headers["etag"] != tag
    stale = await unsafe(
        owner, "PATCH", f"/api/v1/workspaces/{ws}", json={"name": "Again"}, headers={"If-Match": tag}
    )
    assert stale.status_code == 409 and code(stale) == "VERSION_CONFLICT"


# ---------------------------------------------------------------- invitations
async def test_invitation_requires_matching_verified_email_and_is_single_use(new_user: NewUser) -> None:
    owner, invitee, impostor = await new_user(), await new_user(), await new_user()
    unverified = await new_user(email=None)
    ws = await create_workspace(owner)
    created = await invite(owner, ws, invitee.email.upper(), "editor")  # type: ignore[attr-defined]
    assert created.status_code == 201 and created.json()["email"] == invitee.email  # type: ignore[attr-defined]
    token = token_of(created)
    for wrong in (impostor, unverified):
        refused = await accept(wrong, token)
        assert refused.status_code == 403 and code(refused) == "INVITATION_EMAIL_MISMATCH"
    assert (await accept(invitee, token)).json() == {"workspace_id": ws}
    again = await accept(invitee, token)
    assert again.status_code == 404 and code(again) == "INVITATION_INVALID"
    roles = {
        m["user_id"]: m["role"] for m in (await owner.get(f"/api/v1/workspaces/{ws}/members")).json()["items"]
    }
    assert roles[await me(invitee)] == "editor" and await me(impostor) not in roles


async def test_revoked_expired_and_forged_invitations_are_invalid(new_user: NewUser) -> None:
    owner, invitee = await new_user(), await new_user()
    ws = await create_workspace(owner)
    revoked = await invite(owner, ws, invitee.email, "viewer")  # type: ignore[attr-defined]
    assert (
        await unsafe(owner, "DELETE", f"/api/v1/workspaces/{ws}/invitations/{revoked.json()['id']}")
    ).status_code == 204
    assert code(await accept(invitee, token_of(revoked))) == "INVITATION_INVALID"
    expired = await invite(owner, ws, invitee.email, "viewer")  # type: ignore[attr-defined]
    ddl(
        "UPDATE invitations SET expires_at = now() - interval '1 second' WHERE id = :i",
        i=expired.json()["id"],
    )
    assert code(await accept(invitee, token_of(expired))) == "INVITATION_INVALID"
    assert code(await accept(invitee, secrets.token_urlsafe(32))) == "INVITATION_INVALID"


async def test_admins_invite_only_viewers_and_editors(new_user: NewUser) -> None:
    owner, admin = await new_user(), await new_user()
    ws = await create_workspace(owner)
    await join(owner, ws, admin, "admin")
    assert (await invite(admin, ws, "e@example.test", "editor")).status_code == 201
    for role in ("admin", "owner"):
        assert (await invite(admin, ws, f"{role}@example.test", role)).status_code == 403
        assert (await invite(owner, ws, f"{role}@example.test", role)).status_code == 201
    duplicate = await invite(owner, ws, "e@example.test", "viewer")
    assert duplicate.status_code == 409 and code(duplicate) == "INVITATION_EXISTS"


# ---------------------------------------------------------------- roles and removal
async def test_role_matrix(new_user: NewUser) -> None:
    owner, admin, editor, viewer = [await new_user() for _ in range(4)]
    ws = await create_workspace(owner)
    for person, role in ((admin, "admin"), (editor, "editor"), (viewer, "viewer")):
        await join(owner, ws, person, role)
    ids = {
        name: await me(p)
        for name, p in (("owner", owner), ("admin", admin), ("editor", editor), ("viewer", viewer))
    }

    async def set_role(actor: httpx.AsyncClient, target: str, role: str) -> int:
        url = f"/api/v1/workspaces/{ws}/members/{ids[target]}"
        return (await unsafe(actor, "PATCH", url, json={"role": role})).status_code

    assert await set_role(admin, "viewer", "editor") == 204  # admin manages viewers/editors
    assert await set_role(admin, "viewer", "admin") == 403  # but cannot grant admin
    assert await set_role(admin, "owner", "viewer") == 403  # nor touch owners
    assert await set_role(editor, "viewer", "viewer") == 403  # editors manage nobody
    assert await set_role(viewer, "editor", "viewer") == 403
    members = f"/api/v1/workspaces/{ws}/members"
    assert (
        await unsafe(admin, "DELETE", f"{members}/{ids['owner']}")
    ).status_code == 403  # admins can't remove owners
    assert await set_role(owner, "admin", "owner") == 204  # owners manage everyone
    assert (await unsafe(owner, "DELETE", f"{members}/{ids['editor']}")).status_code == 204
    # A removed member loses access on the very next request (A17).
    assert (await editor.get(f"/api/v1/workspaces/{ws}")).status_code == 404
    actions = ddl("SELECT action FROM audit_events WHERE workspace_id = :w ORDER BY created_at", w=ws)
    assert {"member.role_changed", "member.removed", "invitation.accepted"} <= {a[0] for a in actions}


async def test_last_owner_cannot_leave_or_be_demoted(new_user: NewUser) -> None:
    owner, second = await new_user(), await new_user()
    ws = await create_workspace(owner)
    owner_id = await me(owner)
    leave = await unsafe(owner, "DELETE", f"/api/v1/workspaces/{ws}/members/{owner_id}")
    demote = await unsafe(
        owner, "PATCH", f"/api/v1/workspaces/{ws}/members/{owner_id}", json={"role": "admin"}
    )
    assert [code(leave), code(demote)] == ["LAST_OWNER", "LAST_OWNER"]
    await join(owner, ws, second, "owner")
    assert (await unsafe(owner, "DELETE", f"/api/v1/workspaces/{ws}/members/{owner_id}")).status_code == 204
    assert (await owner.get(f"/api/v1/workspaces/{ws}")).status_code == 404


async def test_members_pagination_cursor_is_signed_and_scoped(new_user: NewUser) -> None:
    owner, a, b = await new_user(), await new_user(), await new_user()
    ws, other = await create_workspace(owner), await create_workspace(owner)
    await join(owner, ws, a, "viewer")
    await join(owner, ws, b, "viewer")
    seen: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 1, **({"cursor": cursor} if cursor else {})}
        page = (await owner.get(f"/api/v1/workspaces/{ws}/members", params=params)).json()
        seen += [m["user_id"] for m in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
        last_cursor = cursor
    assert len(seen) == 3 and len(set(seen)) == 3
    tampered = last_cursor[:-2] + ("AA" if not last_cursor.endswith("AA") else "BB")
    for bad, url in ((tampered, ws), (last_cursor, other)):  # forged, or replayed on another workspace
        response = await owner.get(f"/api/v1/workspaces/{url}/members", params={"cursor": bad})
        assert response.status_code == 400 and code(response) == "INVALID_CURSOR"


# ---------------------------------------------------------------- database-level owner invariant
def test_concurrent_removal_of_the_last_two_owners_leaves_one(new_user: NewUser) -> None:
    """Two transactions each remove a different owner at the same moment: exactly one may commit."""
    run = secrets.token_hex(4)
    with engine(RUNTIME_URL).begin() as conn:
        ids = [
            conn.execute(
                sa.text(
                    "SELECT app_private.create_user_with_identity("
                    "'github', 'https://github.com', :s, 'O', NULL, NULL)"
                ),
                {"s": f"{run}-{i}"},
            ).scalar_one()
            for i in range(2)
        ]
        conn.execute(sa.text("SELECT set_config('app.user_id', :u, true)"), {"u": str(ids[0])})
        ws = conn.execute(
            sa.text("SELECT app_private.create_workspace('Race', :s)"), {"s": f"race-{run}"}
        ).scalar_one()
        conn.execute(sa.text("SELECT set_config('app.workspace_id', :w, true)"), {"w": str(ws)})
        conn.execute(
            sa.text("INSERT INTO memberships(workspace_id, user_id, role) VALUES (:w, :u, 'owner')"),
            {"w": ws, "u": ids[1]},
        )
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def remove(user_id: Any) -> None:
        with engine(RUNTIME_URL).connect() as conn:
            conn.execute(sa.text("SELECT set_config('app.workspace_id', :w, true)"), {"w": str(ws)})
            conn.execute(sa.text("DELETE FROM memberships WHERE user_id = :u"), {"u": user_id})
            barrier.wait()  # both deletes are done; now race to commit
            try:
                conn.commit()
                outcomes.append("committed")
            except sa.exc.IntegrityError as error:
                outcomes.append(getattr(error.orig.diag, "constraint_name", "?"))  # type: ignore[union-attr]

    threads = [threading.Thread(target=remove, args=(u,)) for u in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(outcomes) == ["committed", "memberships_keep_owner"]
    owners = ddl("SELECT count(*) FROM memberships WHERE workspace_id = :w AND role = 'owner'", w=ws)
    assert owners[0][0] == 1
