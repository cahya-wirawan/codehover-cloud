"""Freshness against the tracked branch (FR-019, A23) and the workspace audit log (FR-023)."""

from __future__ import annotations

import secrets
from typing import Any

import httpx
import pytest

from app.adapters.github.app import GitHubError
from tests.integration.auth_support import FakeIdP, ddl, unsafe
from tests.integration.conftest import needs_db
from tests.integration.test_builds import drive, setup_project
from tests.integration.test_generation import generation_build, runtime_for
from tests.integration.test_github import FakeGitHub, github  # noqa: F401
from tests.integration.test_workspaces import NewUser, join, new_user  # noqa: F401

pytestmark = [pytest.mark.integration, needs_db]


def expire_observation(project: str) -> None:
    ddl(
        "UPDATE projects SET last_observed_at = now() - interval '10 minutes' WHERE id = :p AND last_observed_at IS NOT NULL",
        p=project,
    )


async def test_freshness_compares_the_tracked_branch_with_what_was_built(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, _ws, build, app = await generation_build(client, idp, github, preset="replay-v2")
    project = str(ddl("SELECT project_id FROM builds WHERE id = :b", b=build)[0][0])
    fresh = (await owner.get(f"/api/v1/projects/{project}/freshness")).json()
    assert fresh["state"] == "never_built" and fresh["latest_sha"] == github.commit_sha
    await drive(app, build, runtime=runtime_for(app))
    expire_observation(project)
    current = (await owner.get(f"/api/v1/projects/{project}/freshness")).json()
    assert current["state"] == "current" and current["latest_ready_sha"] == github.commit_sha

    github.commit_sha = "e" * 40  # the branch moved upstream
    cached = (await owner.get(f"/api/v1/projects/{project}/freshness")).json()
    assert cached["state"] == "current"  # a recent observation (under five minutes) is reused
    expire_observation(project)
    moved = (await owner.get(f"/api/v1/projects/{project}/freshness")).json()
    assert moved["state"] == "update_available" and moved["latest_sha"] == "e" * 40

    async def unreachable(*_args: Any, **_kw: Any) -> str:
        raise GitHubError("timeout", 0)

    monkeypatch.setattr(app.state.github_app, "resolve_ref", unreachable)
    expire_observation(project)
    unknown = (await owner.get(f"/api/v1/projects/{project}/freshness")).json()
    # A23: never claim to be current when GitHub cannot answer; show the last observation instead.
    assert unknown["state"] == "unknown" and unknown["reason"] == "github_unavailable"
    assert (
        unknown["latest_sha"] is None
        and unknown["last_observed_sha"] == "e" * 40
        and unknown["last_observed_at"]
    )
    stranger, _, _ = await setup_project(client, idp, github)
    assert (await stranger.get(f"/api/v1/projects/{project}/freshness")).status_code == 404


async def test_the_audit_log_is_for_admins_newest_first_and_filterable(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, new_user: NewUser
) -> None:
    owner, ws, project = await setup_project(client, idp, github)
    started = await unsafe(
        owner,
        "POST",
        f"/api/v1/projects/{project}/builds",
        json={},
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )
    assert started.status_code == 202
    log = (await owner.get(f"/api/v1/workspaces/{ws}/audit", params={"limit": 2})).json()
    assert log["items"][0]["action"] == "build.requested" and log["items"][0]["actor_name"]
    assert log["next_cursor"]
    older = (
        await owner.get(f"/api/v1/workspaces/{ws}/audit", params={"limit": 2, "cursor": log["next_cursor"]})
    ).json()
    assert {e["id"] for e in older["items"]}.isdisjoint({e["id"] for e in log["items"]})
    projects_only = (await owner.get(f"/api/v1/workspaces/{ws}/audit", params={"action": "project."})).json()
    assert projects_only["items"] and all(e["action"].startswith("project.") for e in projects_only["items"])
    replayed = await owner.get(
        f"/api/v1/workspaces/{ws}/audit", params={"action": "project.", "cursor": log["next_cursor"]}
    )
    assert replayed.status_code == 400  # a cursor is bound to its filter

    editor = await new_user()
    await join(owner, ws, editor, "editor")
    assert (await editor.get(f"/api/v1/workspaces/{ws}/audit")).status_code == 403
    stranger, _, _ = await setup_project(client, idp, github)
    assert (await stranger.get(f"/api/v1/workspaces/{ws}/audit")).status_code == 404
