"""Project, workspace and account deletion, purge and tombstone replay (T17; 01 FR-021, 08, A22).

M4 exit criterion (with the restore drill): deletion stops serving at once, fences running work, purges
content and objects, and a restored database honours the tombstone.
"""

from __future__ import annotations

import json
import secrets
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.engine.keys import TOMBSTONE_PREFIX, project_prefix
from app.jobs import store
from app.jobs.envelope import TaskEnvelope
from app.jobs.purge import purge_due, purge_once
from app.ops import tombstones
from tests.integration.auth_support import FakeIdP, ddl, unsafe
from tests.integration.conftest import needs_db
from tests.integration.test_budgets import make_estimate, start_from
from tests.integration.test_builds import drive, setup_project
from tests.integration.test_generation import runtime_for
from tests.integration.test_github import FakeGitHub, github  # noqa: F401
from tests.integration.test_publication import address, publish, ready_build
from tests.integration.test_workspaces import NewUser, join, new_user  # noqa: F401

pytestmark = [pytest.mark.integration, needs_db]

CONTENT = (
    "source_files f JOIN snapshots s ON s.id = f.snapshot_id WHERE s.project_id = :p",
    "symbols x JOIN builds b ON b.id = x.build_id WHERE b.project_id = :p",
    "file_explanations x JOIN builds b ON b.id = x.build_id WHERE b.project_id = :p",
    "guide_pages x JOIN builds b ON b.id = x.build_id WHERE b.project_id = :p",
    "search_documents x JOIN builds b ON b.id = x.build_id WHERE b.project_id = :p",
    "artifacts x JOIN builds b ON b.id = x.build_id WHERE b.project_id = :p",
    "exports x JOIN builds b ON b.id = x.build_id WHERE b.project_id = :p",
    "publications x WHERE x.project_id = :p",
)


def content_rows(project: str) -> dict[str, int]:
    counts = {}
    for q in CONTENT:  # fixed queries, no input
        counts[q.split()[0]] = ddl(f"SELECT count(*) FROM {q}", p=project)[0][0]  # noqa: S608
    return counts


def objects_under(app: Any, workspace: str, project: str) -> list[str]:
    prefix = project_prefix(UUID(workspace), UUID(project))
    return [k for k in app.state.objects.objects if k.startswith(prefix)]


def make_due(*, project_id: str | None = None, workspace_id: str | None = None) -> None:
    """Skip the wait for fenced leases to expire."""
    ddl(
        "UPDATE deletion_requests SET not_before = now() - interval '1 second'"
        " WHERE project_id = :p OR (scope = 'workspace' AND workspace_id = :w)",
        p=project_id,
        w=workspace_id,
    )


async def delete_project(person: httpx.AsyncClient, project: str, confirmation: str) -> httpx.Response:
    return await unsafe(
        person, "POST", f"/api/v1/projects/{project}/deletion", json={"confirmation": confirmation}
    )


async def test_project_deletion_stops_serving_fences_work_and_purges(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, new_user: NewUser
) -> None:
    owner, ws, project, build, app = await ready_build(client, idp, github)
    published = await publish(
        owner,
        project,
        build_id=build,
        visibility="public",
        public_slug=f"gone-{secrets.token_hex(3)}",
        confirmation="API",
    )
    assert published.status_code == 200, published.text
    slug = published.json()["address"]
    export = await unsafe(
        owner,
        "POST",
        f"/api/v1/builds/{build}/exports",
        json={"format": "kb_json"},
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )
    await drive(app, build, runtime=runtime_for(app))
    export_id = export.json()["id"]
    assert (await owner.get(f"/api/v1/exports/{export_id}/download")).status_code == 200

    # A second build is running: a worker holds the lease of its first task.
    snapshot = ddl("SELECT id FROM snapshots WHERE project_id = :p LIMIT 1", p=project)[0][0]
    estimate = (await make_estimate(owner, project, str(snapshot), preset="replay-v2", guide_pages=3)).json()
    running = (await start_from(owner, project, estimate["id"])).json()["id"]
    payload = ddl(
        "UPDATE outbox SET published_at = now() WHERE published_at IS NULL AND payload->>'build_id' = :b"
        " RETURNING payload",
        b=running,
    )[0][0]
    rt = runtime_for(app)
    async with rt.sessions() as db:
        claimed = await store.claim(db, TaskEnvelope.model_validate(payload))
    assert claimed is not None

    editor = await new_user()
    await join(owner, ws, editor, "editor")
    assert (await delete_project(editor, project, "API")).status_code == 403  # admins only (09)
    mismatch = await delete_project(owner, project, "api")
    assert mismatch.status_code == 422 and mismatch.json()["error"]["code"] == "CONFIRMATION_MISMATCH"
    deleted = await delete_project(owner, project, "API")
    assert deleted.status_code == 202, deleted.text
    body = deleted.json()
    assert (
        body["scope"] == "project"
        and body["state"] == "pending"
        and body["purge_after"] > body["requested_at"]
    )

    # Serving stops in the same transaction (A22).
    assert (await owner.get(f"/api/v1/projects/{project}")).status_code == 404
    assert (await owner.get(f"/api/v1/builds/{build}")).status_code == 404
    assert (await owner.get(f"/api/v1/exports/{export_id}/download")).status_code == 404
    assert (await client.get(f"/api/v1/published/{slug}")).status_code == 404
    listed = (await owner.get(f"/api/v1/workspaces/{ws}/projects")).json()["items"]
    assert project not in {p["id"] for p in listed}
    assert (await delete_project(owner, project, "API")).status_code == 404  # already gone

    # The running build is cancelled with its envelope released; the worker's lease is gone.
    assert ddl("SELECT status FROM builds WHERE id = :b", b=running)[0][0] == "cancelled"
    async with rt.sessions() as db:
        assert not await store.heartbeat(db, claimed)
    async with rt.sessions() as db:
        with pytest.raises(store.LeaseLost):
            await store.complete(db, claimed)
    reserved = ddl("SELECT reserved_micro_usd FROM builds WHERE id = :b", b=running)[0][0]
    assert reserved == 0
    assert ddl("SELECT action FROM audit_events WHERE workspace_id = :w AND action = 'project.deleted'", w=ws)

    # Purge waits for fenced leases to expire, then removes content and objects; accounting rows stay.
    tombstone_key = f"{TOMBSTONE_PREFIX}{body['id']}.json"
    assert json.loads(app.state.objects.objects[tombstone_key][0])["project_id"] == project
    assert objects_under(app, ws, project) and any(content_rows(project).values())
    before = ddl(
        "SELECT count(*) FROM deletion_requests WHERE project_id = :p AND state = 'pending'", p=project
    )
    assert before[0][0] == 1
    make_due(project_id=project)
    assert await purge_due(rt.sessions, app.state.objects) >= 1
    assert ddl("SELECT state FROM deletion_requests WHERE project_id = :p", p=project)[0][0] == "completed"
    assert content_rows(project) == dict.fromkeys(content_rows(project), 0)
    assert objects_under(app, ws, project) == []
    assert ddl("SELECT count(*) FROM builds WHERE project_id = :p", p=project)[0][0] == 2  # ledger references
    assert ddl("SELECT name FROM projects WHERE id = :p", p=project)[0][0] == "Deleted project"
    assert tombstone_key in app.state.objects.objects  # the tombstone outlives the purge

    # The address is free again.
    repo = ddl("SELECT repository_id FROM projects WHERE id = :p", p=project)[0][0]
    again = await unsafe(
        owner,
        "POST",
        f"/api/v1/workspaces/{ws}/projects",
        json={"repository_id": str(repo), "name": "API", "slug": "api"},
    )
    assert again.status_code == 201, again.text


async def test_purge_is_retried_after_a_failure(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, _ws, project = await setup_project(client, idp, github)
    assert (await delete_project(owner, project, "API")).status_code == 202
    make_due(project_id=project)
    app = client.app  # type: ignore[attr-defined]
    rt = runtime_for(app)

    async def broken(_prefix: str) -> int:
        raise ConnectionError("object store down")

    monkeypatch.setattr(app.state.objects, "delete_prefix", broken)
    outcome = await purge_once(rt.sessions, app.state.objects)
    assert outcome is not None and outcome["outcome"] == "failed"
    state = ddl(
        "SELECT state, error_code, attempts, id FROM deletion_requests WHERE project_id = :p", p=project
    )[0]
    assert state[:3] == ("purging", "purge_connectionerror", 1)
    state_id = str(state[3])
    while (other := await purge_once(rt.sessions, app.state.objects)) is not None:
        assert other["deletion_id"] != state_id  # still leased: not claimed again yet
    monkeypatch.undo()
    ddl(
        "UPDATE deletion_requests SET lease_until = now() - interval '1 second' WHERE project_id = :p",
        p=project,
    )
    retried = await purge_once(rt.sessions, app.state.objects)
    assert retried is not None and retried["outcome"] == "completed" and retried["attempt"] == 2


async def test_a_restored_database_reapplies_tombstones_before_serving(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, ws, project, _build, app = await ready_build(client, idp, github)
    deleted = (await delete_project(owner, project, "API")).json()
    # Restore from a backup taken just before the deletion: the request and the tombstone mark are gone.
    ddl("DELETE FROM deletion_requests WHERE id = :d", d=deleted["id"])
    ddl("UPDATE projects SET deleted_at = NULL, name = 'API' WHERE id = :p", p=project)
    assert (await owner.get(f"/api/v1/projects/{project}")).status_code == 200  # resurrected

    rt = runtime_for(app)
    app.state.objects.objects[f"{TOMBSTONE_PREFIX}junk.json"] = (b"{not json", "application/json")
    result = await tombstones.reapply(rt.sessions, app.state.objects)
    assert result["applied"] >= 1 and result["invalid"] >= 1  # unreadable entries are reported, not skipped
    del app.state.objects.objects[f"{TOMBSTONE_PREFIX}junk.json"]
    assert (await owner.get(f"/api/v1/projects/{project}")).status_code == 404
    request = ddl(
        "SELECT id, state, not_before <= now() FROM deletion_requests WHERE project_id = :p", p=project
    )
    assert [(str(r[0]), r[1], r[2]) for r in request] == [
        (deleted["id"], "pending", True)
    ]  # same ID, due now
    assert await purge_due(rt.sessions, app.state.objects) >= 1
    assert content_rows(project) == dict.fromkeys(content_rows(project), 0)

    # A completed purge is reset too: restored object storage may hold copies the purge had removed.
    app.state.objects.objects[project_prefix(UUID(ws), UUID(project)) + "late"] = (b"x", "text/plain")
    await tombstones.reapply(rt.sessions, app.state.objects)
    assert ddl("SELECT state FROM deletion_requests WHERE project_id = :p", p=project)[0][0] == "pending"
    await purge_due(rt.sessions, app.state.objects)
    assert objects_under(app, ws, project) == []


async def test_workspace_deletion_is_for_owners_and_removes_every_project(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, new_user: NewUser
) -> None:
    owner, ws, project = await setup_project(client, idp, github)
    slug = (await owner.get(f"/api/v1/workspaces/{ws}")).json()["slug"]
    admin = await new_user()
    await join(owner, ws, admin, "admin")
    url = f"/api/v1/workspaces/{ws}/deletion"
    assert (await unsafe(admin, "POST", url, json={"confirmation": slug})).status_code == 403
    assert (await unsafe(owner, "POST", url, json={"confirmation": "nope"})).status_code == 422
    deleted = await unsafe(owner, "POST", url, json={"confirmation": slug})
    assert deleted.status_code == 202 and deleted.json()["scope"] == "workspace"
    assert (await owner.get(f"/api/v1/workspaces/{ws}")).status_code == 404
    assert (await admin.get(f"/api/v1/projects/{project}")).status_code == 404
    assert ws not in {w["id"] for w in (await owner.get("/api/v1/workspaces")).json()["items"]}

    app = client.app  # type: ignore[attr-defined]
    make_due(workspace_id=ws)
    await purge_due(runtime_for(app).sessions, app.state.objects)
    assert ddl("SELECT count(*) FROM memberships WHERE workspace_id = :w", w=ws)[0][0] == 0
    assert ddl("SELECT name, status FROM workspaces WHERE id = :w", w=ws)[0] == (
        "Deleted workspace",
        "deleting",
    )
    assert ddl("SELECT status FROM repository_connections WHERE workspace_id = :w", w=ws)[0][0] == "revoked"
    assert ddl("SELECT state FROM deletion_requests WHERE workspace_id = :w", w=ws)[0][0] == "completed"


async def test_account_deletion_needs_a_recent_sign_in_and_resolves_ownership(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, new_user: NewUser
) -> None:
    owner, ws, project = await setup_project(client, idp, github)
    member = await new_user()
    await join(owner, ws, member, "editor")
    me = (await owner.get("/api/v1/me")).json()

    exported = await unsafe(owner, "POST", "/api/v1/me/export")
    assert exported.status_code == 200 and exported.headers["cache-control"] == "no-store"
    data = exported.json()
    assert data["profile"]["id"] == me["id"]
    assert [p["id"] for w in data["workspaces"] for p in w["projects"]] == [project]
    assert member.email not in exported.text  # type: ignore[attr-defined]  # no other member's account data

    url = "/api/v1/me/deletion"
    ddl(
        "UPDATE sessions SET reauthenticated_at = now() - interval '11 minutes' WHERE user_id = :u",
        u=me["id"],
    )
    stale = await unsafe(owner, "POST", url, json={"confirmation": "delete my account"})
    assert stale.status_code == 403 and stale.json()["error"]["code"] == "REAUTH_REQUIRED"
    ddl("UPDATE sessions SET reauthenticated_at = now() WHERE user_id = :u", u=me["id"])
    assert (await unsafe(owner, "POST", url, json={"confirmation": "yes"})).status_code == 422
    sole = await unsafe(owner, "POST", url, json={"confirmation": "delete my account"})
    assert sole.status_code == 409 and sole.json()["error"]["code"] == "OWNERSHIP_TRANSFER_REQUIRED"
    assert [w["id"] for w in sole.json()["error"]["details"]["workspaces"]] == [ws]

    # A member who owns nothing just leaves; the workspace keeps working.
    member_me = (await member.get("/api/v1/me")).json()
    left = await unsafe(member, "POST", url, json={"confirmation": "delete my account"})
    assert left.status_code == 202 and left.json()["workspaces"] == []
    assert (await member.get("/api/v1/me")).status_code == 401  # signed out everywhere
    assert ddl("SELECT count(*) FROM memberships WHERE user_id = :u", u=member_me["id"])[0][0] == 0
    assert (await owner.get(f"/api/v1/projects/{project}")).status_code == 200

    gone = await unsafe(
        owner, "POST", url, json={"confirmation": "delete my account", "delete_workspaces": [ws]}
    )
    assert gone.status_code == 202, gone.text
    assert [w["scope"] for w in gone.json()["workspaces"]] == ["workspace"]
    assert "set-cookie" in gone.headers  # the session cookie is cleared
    assert (await owner.get("/api/v1/me")).status_code == 401
    user = ddl("SELECT status, display_name, primary_email FROM users WHERE id = :u", u=me["id"])[0]
    assert user == ("deleted", "Deleted user", None)
    assert ddl("SELECT count(*) FROM oauth_identities WHERE user_id = :u", u=me["id"])[0][0] == 0
    assert (
        ddl("SELECT action FROM audit_events WHERE workspace_id IS NULL AND actor_user_id = :u", u=me["id"])[
            -1
        ][0]
        == "account.deleted"
    )


async def test_an_operator_takedown_survives_project_deletion(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    """Security review of 0015: deleting the project must not free a taken-down public address."""
    import sqlalchemy as sa

    owner, _ws, project, build, app = await ready_build(client, idp, github)
    slug = await address(owner, project)
    published = await publish(
        owner, project, build_id=build, visibility="public", public_slug=slug, confirmation="API"
    )
    assert published.status_code == 200, published.text
    ddl(
        "UPDATE publications SET suspended_at = now(), suspended_reason = 'operator' WHERE project_id = :p",
        p=project,
    )
    assert (await delete_project(owner, project, "API")).status_code == 202
    make_due(project_id=project)
    await purge_due(runtime_for(app).sessions, app.state.objects)
    assert ddl("SELECT state FROM deletion_requests WHERE project_id = :p", p=project)[0][0] == "completed"
    kept = ddl("SELECT suspended_reason FROM publications WHERE project_id = :p", p=project)
    assert kept == [("operator",)]  # the takedown remains (its address names this owner's project)
    with pytest.raises(sa.exc.IntegrityError):
        ddl("DELETE FROM publications WHERE project_id = :p", p=project)  # the 0012 trigger, again
    assert (await client.get(f"/api/v1/published/{slug}")).status_code == 404
