"""Per-project access control (0027; 01 "per-project ACLs are P1", 09).

A restricted project is invisible (404) to workspace members without a grant on every path that reaches
project content: the project, its builds, viewer data, freshness, exports, the event stream and its
members-only site. Admins and owners always see it; a grant is the member's role on that project.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.integration.auth_support import FakeIdP, ddl, unsafe
from tests.integration.conftest import needs_db
from tests.integration.test_budgets import make_estimate
from tests.integration.test_github import FakeGitHub, github  # noqa: F401
from tests.integration.test_operations import operator  # noqa: F401
from tests.integration.test_publication import address, publish, ready_build
from tests.integration.test_workspaces import NewUser, code, join, new_user  # noqa: F401

pytestmark = [pytest.mark.integration, needs_db]


async def set_mode(person: httpx.AsyncClient, project: str, mode: str) -> httpx.Response:
    current = await person.get(f"/api/v1/projects/{project}/access")
    return await unsafe(
        person,
        "PUT",
        f"/api/v1/projects/{project}/access",
        json={"mode": mode},
        headers={"If-Match": current.headers.get("etag", '"none"')},
    )


async def grant(admin: httpx.AsyncClient, project: str, user: str, role: str) -> httpx.Response:
    return await unsafe(
        admin, "PUT", f"/api/v1/projects/{project}/access/members/{user}", json={"role": role}
    )


async def user_id(person: httpx.AsyncClient) -> str:
    value: str = (await person.get("/api/v1/me")).json()["id"]
    return value


def project_paths(project: str, build: str) -> list[str]:
    return [
        f"/api/v1/projects/{project}",
        f"/api/v1/projects/{project}/builds",
        f"/api/v1/projects/{project}/freshness",
        f"/api/v1/projects/{project}/push-policy",
        f"/api/v1/builds/{build}",
        f"/api/v1/builds/{build}/tree",
        f"/api/v1/builds/{build}/guide",
        f"/api/v1/builds/{build}/search?q=main",
        f"/api/v1/builds/{build}/variants",
        f"/api/v1/builds/{build}/events?once=true",
    ]


async def visible(person: httpx.AsyncClient, project: str, build: str) -> dict[str, int]:
    return {path: (await person.get(path)).status_code for path in project_paths(project, build)}


async def test_a_restricted_project_is_invisible_without_a_grant_and_a_grant_is_the_role(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, new_user: NewUser
) -> None:
    owner, ws, project, build, _app = await ready_build(client, idp, github)
    slug = await address(owner, project)
    assert (
        await publish(owner, project, build_id=build, public_slug=slug)
    ).status_code == 200  # members only
    editor, viewer, admin = await new_user(), await new_user(), await new_user()
    for person, role in ((editor, "editor"), (viewer, "viewer"), (admin, "admin")):
        await join(owner, ws, person, role)
    listed = lambda person: person.get(f"/api/v1/workspaces/{ws}/projects")  # noqa: E731

    # Open by default: every member reads it.
    assert all(
        code < 400 for path, code in (await visible(editor, project, build)).items() if "events" not in path
    )
    assert (await editor.get(f"/api/v1/projects/{project}/access")).status_code == 403  # admins manage access
    assert (await set_mode(editor, project, "restricted")).status_code == 403
    no_etag = await unsafe(owner, "PUT", f"/api/v1/projects/{project}/access", json={"mode": "restricted"})
    assert no_etag.status_code == 428
    restricted = await set_mode(owner, project, "restricted")
    assert restricted.status_code == 200 and restricted.json()["mode"] == "restricted"

    for person in (editor, viewer):
        assert set((await visible(person, project, build)).values()) == {404}
        assert [p["id"] for p in (await listed(person)).json()["items"]] == []
        assert (await person.get(f"/api/v1/published/{slug}")).status_code == 404
        estimate = await make_estimate(person, project, "00000000-0000-7000-8000-000000000000")
        assert estimate.status_code == 404
    for person in (owner, admin):
        assert (await person.get(f"/api/v1/builds/{build}/tree")).status_code == 200
        items = (await listed(person)).json()["items"]
        assert [(p["id"], p["access"]) for p in items] == [(project, "restricted")]
        assert (await person.get(f"/api/v1/published/{slug}")).status_code == 200

    # A grant is the member's role on this project, below or above their workspace role.
    editor_id, viewer_id, admin_id = await user_id(editor), await user_id(viewer), await user_id(admin)
    assert (await grant(owner, project, editor_id, "viewer")).status_code == 200
    assert (await grant(admin, project, viewer_id, "editor")).status_code == 200
    assert (await editor.get(f"/api/v1/builds/{build}/tree")).status_code == 200
    assert (await editor.get(f"/api/v1/published/{slug}")).status_code == 200
    as_viewer = await unsafe(editor, "POST", f"/api/v1/projects/{project}/snapshots", json={})
    assert as_viewer.status_code == 403  # an editor of the workspace, but a viewer here
    snapshot = str(ddl("SELECT snapshot_id FROM builds WHERE id = :b", b=build)[0][0])
    as_editor = await make_estimate(viewer, project, snapshot, preset="replay-v2")
    assert as_editor.status_code == 201, as_editor.text  # a viewer of the workspace, an editor here
    assert [p["id"] for p in (await listed(viewer)).json()["items"]] == [project]

    assert code(await grant(owner, project, admin_id, "viewer")) == "ALREADY_FULL_ACCESS"
    assert (await grant(owner, project, "00000000-0000-7000-8000-000000000000", "viewer")).status_code == 404
    access = (await owner.get(f"/api/v1/projects/{project}/access")).json()
    assert {(m["user_id"], m["role"], m["workspace_role"]) for m in access["members"]} == {
        (editor_id, "viewer", "editor"),
        (viewer_id, "editor", "viewer"),
    }

    # Revoking and leaving the workspace take access away at once.
    assert (
        await unsafe(owner, "DELETE", f"/api/v1/projects/{project}/access/members/{editor_id}")
    ).status_code == 204
    assert (await editor.get(f"/api/v1/builds/{build}/tree")).status_code == 404
    assert (await unsafe(owner, "DELETE", f"/api/v1/workspaces/{ws}/members/{viewer_id}")).status_code == 204
    assert ddl("SELECT count(*) FROM project_members WHERE user_id = :u", u=viewer_id)[0][0] == 0
    actions = [
        r[0]
        for r in ddl(
            "SELECT action FROM audit_events WHERE workspace_id = :w AND action LIKE 'project.%' ORDER BY created_at",
            w=ws,
        )
    ]
    assert actions[-4:] == [
        "project.access_changed",
        "project.member_granted",
        "project.member_granted",
        "project.member_revoked",
    ]

    # Opening it again restores workspace-wide access; the remaining grants stay for a later restriction.
    assert (await set_mode(owner, project, "workspace")).status_code == 200
    assert (await editor.get(f"/api/v1/builds/{build}/tree")).status_code == 200


async def test_support_access_never_reaches_a_restricted_project(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, operator: httpx.AsyncClient
) -> None:
    owner, ws, project, build, _app = await ready_build(client, idp, github)
    granted = await unsafe(
        owner, "POST", f"/api/v1/workspaces/{ws}/support-access", json={"hours": 1, "reason": "debug a build"}
    )
    assert granted.status_code in (200, 201), granted.text
    assert (await operator.get(f"/api/v1/builds/{build}/tree")).status_code == 200
    assert (await set_mode(owner, project, "restricted")).status_code == 200
    assert set((await visible(operator, project, build)).values()) == {404}
    assert (await operator.get(f"/api/v1/workspaces/{ws}/projects")).json()["items"] == []


def test_projects_rows_are_filtered_for_users_but_not_for_workers(world: Any, runtime: Any) -> None:
    """The row policy backstop: with a user in the context, a restricted project is invisible unless that
    user may see it; workers (no user) see every project of their workspace."""
    stranger = ddl("INSERT INTO users(display_name) VALUES ('Viewer') RETURNING id")[0][0]
    ddl(
        "INSERT INTO memberships(workspace_id, user_id, role) VALUES (:w, :u, 'viewer')",
        w=world.a.workspace,
        u=stranger,
    )
    try:
        ddl("UPDATE projects SET access = 'restricted' WHERE id = :p", p=world.a.project)
        runtime.context(workspace=world.a.workspace, user=stranger)
        assert runtime.one("SELECT count(*) FROM projects WHERE id = :p", p=world.a.project) == 0
        runtime.context(workspace=world.a.workspace, user=world.a.user)  # the owner
        assert runtime.one("SELECT count(*) FROM projects WHERE id = :p", p=world.a.project) == 1
        runtime.context(workspace=world.a.workspace)  # a worker
        assert runtime.one("SELECT count(*) FROM projects WHERE id = :p", p=world.a.project) == 1
        ddl(
            "INSERT INTO project_members(workspace_id, project_id, user_id, role, granted_by)"
            " VALUES (:w, :p, :u, 'viewer', :by)",
            w=world.a.workspace,
            p=world.a.project,
            u=stranger,
            by=world.a.user,
        )
        runtime.context(workspace=world.a.workspace, user=stranger)
        assert runtime.one("SELECT count(*) FROM projects WHERE id = :p", p=world.a.project) == 1
        assert (
            runtime.one(
                "SELECT app_private.project_role(:w, :p, :u)",
                w=world.a.workspace,
                p=world.a.project,
                u=stranger,
            )
            == "viewer"
        )
    finally:
        ddl("DELETE FROM project_members WHERE user_id = :u", u=stranger)
        ddl("DELETE FROM memberships WHERE user_id = :u", u=stranger)
        ddl("UPDATE projects SET access = 'workspace' WHERE id = :p", p=world.a.project)
