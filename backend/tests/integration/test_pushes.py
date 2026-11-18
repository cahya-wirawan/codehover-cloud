"""Push rebuilds (M6, T21; 01 FR-025): debounce, deduplication, policies and never publishing.

The exit gate: repeated pushes schedule one appropriate build, and nothing a push starts is published.
"""

from __future__ import annotations

import secrets
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.modules.projects import pushes
from tests.integration.auth_support import FakeIdP, ddl, unsafe
from tests.integration.conftest import needs_db
from tests.integration.test_budgets import drive_snapshot
from tests.integration.test_builds import setup_project
from tests.integration.test_github import FakeGitHub, github  # noqa: F401
from tests.integration.test_publication import ready_build
from tests.integration.test_webhooks_dispatch import SECRET, signed
from tests.integration.test_workspaces import NewUser, code, join, new_user  # noqa: F401

pytestmark = [pytest.mark.integration, needs_db]


def configure(client: httpx.AsyncClient, debounce: int = 0) -> Any:
    app = client.app  # type: ignore[attr-defined]
    app.state.settings = app.state.settings.model_copy(
        update={"github_webhook_secret": SecretStr(SECRET), "push_debounce_seconds": debounce}
    )
    return app


def push_body(
    installation: int, repo: int, sha: str, ref: str = "refs/heads/main", **extra: Any
) -> dict[str, Any]:
    return {
        "ref": ref,
        "after": sha,
        "deleted": False,
        "installation": {"id": installation},
        "repository": {"id": repo},
        **extra,
    }


def source_of(project: str) -> tuple[int, int, str]:
    row = ddl(
        "SELECT c.installation_id, r.provider_repo_id, p.tracked_ref FROM projects p"
        " JOIN repositories r ON r.id = p.repository_id JOIN repository_connections c ON c.id = r.connection_id"
        " WHERE p.id = :p",
        p=project,
    )[0]
    return int(row[0]), int(row[1]), str(row[2])


async def set_policy(person: httpx.AsyncClient, project: str, policy: str, cap: int = 0) -> httpx.Response:
    current = await person.get(f"/api/v1/projects/{project}/push-policy")
    return await unsafe(
        person,
        "PUT",
        f"/api/v1/projects/{project}/push-policy",
        json={"policy": policy, "cap_micro_usd": cap},
        headers={"If-Match": current.headers["etag"]},
    )


def requests_of(project: str) -> list[Any]:
    return ddl(
        "SELECT commit_sha, pushes, state, outcome, snapshot_id, build_id FROM push_requests WHERE project_id = :p"
        " ORDER BY first_received_at",
        p=project,
    )


async def process(app: Any) -> dict[str, int]:
    ddl("UPDATE push_requests SET due_at = now() WHERE state IN ('pending', 'acquiring')")  # skip the clock
    return await pushes.process_due(
        pushes.PushRuntime(app.state.sessions, app.state.settings, app.state.catalog)
    )


def new_sha() -> str:
    return secrets.token_hex(20)


async def test_the_push_policy_is_admin_only_and_versioned(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, new_user: NewUser
) -> None:
    owner, ws, project = await setup_project(client, idp, github)
    initial = await owner.get(f"/api/v1/projects/{project}/push-policy")
    assert initial.status_code == 200 and initial.json() == {
        "policy": "off",
        "cap_micro_usd": 0,
        "set_by_name": None,
        "version": 1,
    }
    no_etag = await unsafe(owner, "PUT", f"/api/v1/projects/{project}/push-policy", json={"policy": "build"})
    assert no_etag.status_code == 428
    editor = await new_user()
    await join(owner, ws, editor, "editor")
    assert (await editor.get(f"/api/v1/projects/{project}/push-policy")).status_code == 200
    assert (await set_policy(editor, project, "build")).status_code == 403
    stale = await unsafe(
        owner,
        "PUT",
        f"/api/v1/projects/{project}/push-policy",
        json={"policy": "snapshot"},
        headers={"If-Match": '"push-policy-0"'},
    )
    assert stale.status_code == 409 and code(stale) == "VERSION_CONFLICT"
    changed = await set_policy(owner, project, "build", 2_000_000)
    assert changed.status_code == 200, changed.text
    assert changed.json()["policy"] == "build" and changed.json()["cap_micro_usd"] == 2_000_000
    assert changed.json()["set_by_name"] and changed.headers["etag"] == '"push-policy-2"'
    assert (
        ddl(
            "SELECT count(*) FROM audit_events WHERE workspace_id = :w AND action = 'project.push_policy_changed'",
            w=ws,
        )[0][0]
        == 1
    )


async def test_pushes_are_filtered_debounced_and_deduplicated(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, project = await setup_project(client, idp, github)
    app = configure(client, debounce=60)
    installation, repo, branch = source_of(project)
    first, second = new_sha(), new_sha()
    # Policy off: a push is recorded and ignored.
    off = await signed(client, push_body(installation, repo, first), event="push")
    assert off.status_code == 202 and off.json()["state"] == "ignored" and requests_of(project) == []
    assert (await set_policy(owner, project, "snapshot")).status_code == 200
    for body in (
        push_body(installation, repo, first, ref="refs/heads/feature"),  # not the tracked branch
        push_body(installation, repo, first, ref=f"refs/tags/{branch}"),  # a tag
        push_body(installation, repo, "0" * 40, deleted=True),  # a deleted branch
        push_body(installation, repo + 999_999, first),  # another repository
        push_body(installation + 999_999, repo, first),  # another installation
    ):
        assert (await signed(client, body, event="push")).json()["state"] == "ignored"
    assert requests_of(project) == []
    delivery = secrets.token_hex(8)
    assert (
        await signed(client, push_body(installation, repo, first), event="push", delivery=delivery)
    ).json()["state"] == "processed"
    assert (
        await signed(client, push_body(installation, repo, first), event="push", delivery=delivery)
    ).json() == {"duplicate": True}
    await signed(client, push_body(installation, repo, second), event="push")
    rows = requests_of(project)
    assert (
        len(rows) == 1 and rows[0][0] == second and rows[0][1] == 2 and rows[0][2] == "pending"
    )  # latest wins
    # Not due yet: the worker leaves it alone.
    assert (
        await pushes.process_due(
            pushes.PushRuntime(app.state.sessions, app.state.settings, app.state.catalog)
        )
        == {}
    )


async def test_the_snapshot_policy_acquires_the_pushed_commit_once(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, project = await setup_project(client, idp, github)
    app = configure(client)
    installation, repo, _ = source_of(project)
    assert (await set_policy(owner, project, "snapshot")).status_code == 200
    sha = new_sha()
    github.commit_sha = sha
    await signed(client, push_body(installation, repo, sha), event="push")
    await process(app)
    [(commit, _, state, outcome, snapshot, build)] = requests_of(project)
    assert (commit, state, outcome, build) == (sha, "done", "snapshot_requested", None)
    assert await drive_snapshot(app, str(snapshot)) == ["completed"]
    assert ddl("SELECT status, commit_sha FROM snapshots WHERE id = :s", s=snapshot)[0] == ("scanned", sha)
    await signed(client, push_body(installation, repo, sha), event="push")  # the same commit again
    await process(app)
    assert requests_of(project)[-1][2:4] == ("skipped", "already_built")
    actor = ddl(
        "SELECT actor_user_id FROM audit_events WHERE resource_id = :s AND action = 'snapshot.requested'",
        s=snapshot,
    )[0][0]
    assert str(actor) == (await owner.get("/api/v1/me")).json()["id"]  # on behalf of the policy's admin


async def test_the_build_policy_builds_a_replay_project_and_never_publishes(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, project = await setup_project(client, idp, github)
    app = configure(client)
    installation, repo, _ = source_of(project)
    assert (await set_policy(owner, project, "build")).status_code == 200
    sha = new_sha()
    github.commit_sha = sha
    for _ in range(3):  # a burst of pushes: one build
        await signed(client, push_body(installation, repo, sha), event="push")
    await process(app)
    [(_, pushes_count, state, outcome, _, build)] = requests_of(project)
    assert (pushes_count, state, outcome) == (3, "done", "build_started") and build is not None
    config = ddl("SELECT config FROM builds WHERE id = :b", b=build)[0][0]
    assert config["trigger"] == "push" and config["generator"] == "replay-v1"
    assert ddl("SELECT count(*) FROM builds WHERE project_id = :p", p=project)[0][0] == 1
    assert ddl("SELECT count(*) FROM publications WHERE project_id = :p", p=project)[0][0] == 0


async def test_a_generated_project_reuses_its_approved_config_within_the_push_cap(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, ws, project, build, app = await ready_build(client, idp, github)
    configure(client)
    installation, repo, _ = source_of(project)
    assert (await set_policy(owner, project, "build", 0)).status_code == 200
    sha = new_sha()
    github.commit_sha = sha
    await signed(client, push_body(installation, repo, sha), event="push")
    await process(app)
    [(_, _, state, outcome, snapshot, _)] = requests_of(project)
    assert (state, outcome) == ("acquiring", "acquiring")  # estimating needs the scan first
    assert await drive_snapshot(app, str(snapshot)) == ["completed"]
    await process(app)
    [(_, _, state, outcome, _, started)] = requests_of(project)
    assert (state, outcome) == ("done", "build_started"), outcome  # the replay preset costs 0 ≤ cap 0
    estimate = ddl(
        "SELECT e.config->>'model_preset' FROM builds b JOIN estimates e ON e.id = b.estimate_id"
        " WHERE b.id = :b",
        b=started,
    )[0][0]
    assert estimate == "replay-v2"

    # A config whose estimate exceeds the push cap waits for a person.
    ddl("UPDATE builds SET status = 'ready', completed_at = now() WHERE id = :b", b=started)
    ddl(
        "UPDATE estimates SET config = jsonb_set(config, '{model_preset}', '\"balanced-v1\"')"
        " WHERE id = (SELECT estimate_id FROM builds WHERE id = :b)",
        b=started,
    )
    priced = new_sha()
    github.commit_sha = priced
    await signed(client, push_body(installation, repo, priced), event="push")
    await process(app)
    snapshot = requests_of(project)[-1][4]
    assert await drive_snapshot(app, str(snapshot)) == ["completed"]
    await process(app)
    assert requests_of(project)[-1][2:4] == ("skipped", "over_push_cap")
    assert (
        ddl(
            "SELECT count(*) FROM audit_events WHERE workspace_id = :w AND action = 'build.push_needs_approval'",
            w=ws,
        )[0][0]
        == 1
    )
    assert (
        ddl("SELECT count(*) FROM publications WHERE project_id = :p AND build_id <> :b", p=project, b=build)[
            0
        ][0]
        == 0
    )


async def test_work_stops_when_the_policy_admin_loses_the_role(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, new_user: NewUser
) -> None:
    owner, ws, project = await setup_project(client, idp, github)
    app = configure(client)
    installation, repo, _ = source_of(project)
    admin = await new_user()
    await join(owner, ws, admin, "admin")
    assert (await set_policy(admin, project, "build")).status_code == 200
    admin_id = (await admin.get("/api/v1/me")).json()["id"]
    assert (
        await unsafe(owner, "PATCH", f"/api/v1/workspaces/{ws}/members/{admin_id}", json={"role": "editor"})
    ).status_code == 204
    await signed(client, push_body(installation, repo, new_sha()), event="push")
    await process(app)
    assert requests_of(project)[-1][2:4] == ("skipped", "policy_admin_lost")
    assert (await owner.get(f"/api/v1/projects/{project}/push-policy")).json()["policy"] == "off"
    assert ddl("SELECT count(*) FROM builds WHERE project_id = :p", p=project)[0][0] == 0
    assert (
        ddl(
            "SELECT count(*) FROM audit_events WHERE workspace_id = :w AND action = 'project.push_policy_disabled'",
            w=ws,
        )[0][0]
        == 1
    )
    listed = await owner.get(f"/api/v1/projects/{project}/push-requests")
    assert listed.status_code == 200 and listed.json()["items"][0]["outcome"] == "policy_admin_lost"
