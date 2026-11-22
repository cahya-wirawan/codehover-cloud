"""Platform operations (M4; 02 S21, 08 support access, 09 operations role, 11 entitlements, 22 kill switch).

Operators are a role outside tenancy: they see aggregates and identifiers, act through audited database
functions, and read a workspace only under that workspace's own, time-bounded support grant.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa

from tests.integration.auth_support import FakeIdP, ddl, unsafe
from tests.integration.conftest import RUNTIME_URL, engine, needs_db
from tests.integration.test_github import FakeGitHub, github  # noqa: F401
from tests.integration.test_publication import publish, ready_build
from tests.integration.test_workspaces import NewUser, join, new_user  # noqa: F401

pytestmark = [pytest.mark.integration, needs_db]


@pytest_asyncio.fixture
async def operator(new_user: NewUser) -> AsyncIterator[httpx.AsyncClient]:
    person = await new_user()
    me = (await person.get("/api/v1/me")).json()
    ddl("INSERT INTO operators(user_id, note) VALUES (:u, 'test')", u=me["id"])  # granted by the deployment
    try:
        yield person
    finally:
        ddl("UPDATE operators SET revoked_at = now() WHERE user_id = :u", u=me["id"])
        ddl("UPDATE platform_settings SET builds_paused = false, paused_reason = ''")


async def test_operations_are_invisible_to_everyone_else(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, new_user: NewUser
) -> None:
    owner, _ws, project, build, _app = await ready_build(client, idp, github)
    assert (await owner.get("/api/v1/me")).json()["operator"] is False
    for path in ("/api/v1/ops/summary", f"/api/v1/ops/builds/{build}", "/api/v1/ops/events"):
        assert (await owner.get(path)).status_code == 404
    paused = await unsafe(owner, "POST", "/api/v1/ops/builds-paused", json={"paused": True, "reason": "nope"})
    assert paused.status_code == 404
    suspend = await unsafe(
        owner, "POST", f"/api/v1/ops/projects/{project}/suspension", json={"reason": "nope"}
    )
    assert suspend.status_code == 404
    me = (await owner.get("/api/v1/me")).json()["id"]
    # The database refuses too: every ops function re-checks the role from the session's user.
    with engine(RUNTIME_URL).connect() as conn:
        conn.execute(sa.text("SELECT set_config('app.user_id', :u, false)"), {"u": me})
        for call in (
            "SELECT app_private.ops_summary()",
            "SELECT app_private.ops_set_builds_paused(true, 'x')",
        ):
            with pytest.raises(sa.exc.ProgrammingError):
                conn.execute(sa.text(call))
            conn.rollback()
            conn.execute(sa.text("SELECT set_config('app.user_id', :u, false)"), {"u": me})
        with pytest.raises(sa.exc.ProgrammingError):  # internal helper: not callable by the runtime at all
            conn.execute(sa.text("SELECT app_private.ops_event('x', NULL, NULL, 'x')"))


async def test_summary_and_diagnostics_show_ids_states_and_counts_only(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, operator: httpx.AsyncClient
) -> None:
    _owner, _ws, _project, build, _app = await ready_build(client, idp, github)
    assert (await operator.get("/api/v1/me")).json()["operator"] is True
    summary = await operator.get("/api/v1/ops/summary")
    assert summary.status_code == 200
    body = summary.json()
    assert body["builds_paused"] is False and body["builds_last_24h"].get("ready", 0) >= 1
    assert "runnable_tasks" in body["queue"]
    diagnostics = await operator.get(f"/api/v1/ops/builds/{build}")
    assert diagnostics.status_code == 200
    data = diagnostics.json()
    assert data["status"] == "ready" and {t["stage"] for t in data["tasks"]} >= {
        "acquire",
        "explain",
        "render",
    }
    assert any(a["purpose"] == "explain" for a in data["provider_attempts"])
    for content in ("src/", "app.py", "def main", "Entry point", '"API"'):
        assert content not in diagnostics.text, content
    assert (
        await operator.get(f"/api/v1/builds/{build}")
    ).status_code == 404  # no tenant data without a grant


async def test_the_kill_switch_pauses_new_builds_but_not_reading(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, operator: httpx.AsyncClient
) -> None:
    owner, _ws, project, build, _app = await ready_build(client, idp, github)
    short = await unsafe(operator, "POST", "/api/v1/ops/builds-paused", json={"paused": True, "reason": "x"})
    assert short.status_code == 422  # a reason is required
    paused = await unsafe(
        operator, "POST", "/api/v1/ops/builds-paused", json={"paused": True, "reason": "provider incident"}
    )
    assert paused.status_code == 200 and paused.json()["builds_paused"] is True
    blocked = await unsafe(
        owner,
        "POST",
        f"/api/v1/projects/{project}/builds",
        json={},
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )
    assert blocked.status_code == 503 and blocked.json()["error"]["code"] == "BUILDS_PAUSED"
    assert blocked.json()["error"]["retryable"] is True
    assert (await owner.get(f"/api/v1/builds/{build}/tree")).status_code == 200  # reads keep working
    resumed = await unsafe(
        operator, "POST", "/api/v1/ops/builds-paused", json={"paused": False, "reason": "incident over"}
    )
    assert resumed.json()["builds_paused"] is False
    started = await unsafe(
        owner,
        "POST",
        f"/api/v1/projects/{project}/builds",
        json={},
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )
    assert started.status_code == 202
    actions = [e["action"] for e in (await operator.get("/api/v1/ops/events")).json()["items"][:2]]
    assert actions == ["builds.resumed", "builds.paused"]


async def test_operator_takedown_is_audited_in_the_workspace_and_binds_admins(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, operator: httpx.AsyncClient
) -> None:
    owner, ws, project, build, _app = await ready_build(client, idp, github)
    slug = f"ops-{secrets.token_hex(3)}"
    assert (
        await publish(
            owner, project, build_id=build, visibility="public", public_slug=slug, confirmation="API"
        )
    ).status_code == 200
    url = f"/api/v1/ops/projects/{project}/suspension"
    assert (await unsafe(operator, "POST", url, json={"reason": "reported abuse"})).status_code == 204
    assert (await client.get(f"/api/v1/published/{slug}")).status_code == 404
    etag = (await owner.get(f"/api/v1/projects/{project}/publication")).headers["etag"]
    refused = await unsafe(
        owner, "DELETE", f"/api/v1/projects/{project}/publication", headers={"If-Match": etag}
    )
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "SUSPENDED_BY_OPERATOR"
    audit = (await owner.get(f"/api/v1/workspaces/{ws}/audit", params={"action": "publication."})).json()
    suspended = next(e for e in audit["items"] if e["action"] == "publication.suspended")
    assert suspended["metadata"] == {"operator": True, "reason": "reported abuse"}
    assert (await unsafe(operator, "DELETE", url, params={"reason": "resolved"})).status_code == 204
    assert (await client.get(f"/api/v1/published/{slug}")).status_code == 200
    missing = await unsafe(operator, "POST", f"/api/v1/ops/projects/{ws}/suspension", json={"reason": "typo"})
    assert missing.status_code == 404


async def test_an_operator_can_end_a_slug_reservation_early(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, operator: httpx.AsyncClient
) -> None:
    """0036: an unpublished address stays reserved for its workspace until an operator releases it."""
    owner, ws, project, build, _app = await ready_build(client, idp, github)
    slug = f"held-{secrets.token_hex(3)}"
    v1 = await publish(
        owner, project, build_id=build, visibility="public", public_slug=slug, confirmation="API"
    )
    assert v1.status_code == 200
    removed = await unsafe(
        owner, "DELETE", f"/api/v1/projects/{project}/publication", headers={"If-Match": v1.headers["etag"]}
    )
    assert removed.status_code == 204
    assert [str(r[0]) for r in ddl("SELECT workspace_id FROM slug_reservations WHERE slug = :s", s=slug)] == [
        ws
    ]
    url = f"/api/v1/ops/slug-reservations/{slug}"
    assert (
        await unsafe(owner, "DELETE", url, params={"reason": "mine"})
    ).status_code == 404  # operators only
    assert (await unsafe(operator, "DELETE", url, params={"reason": "disputed address"})).status_code == 204
    assert ddl("SELECT count(*) FROM slug_reservations WHERE slug = :s", s=slug)[0][0] == 0
    assert (await unsafe(operator, "DELETE", url, params={"reason": "again"})).status_code == 404
    audit = (await owner.get(f"/api/v1/workspaces/{ws}/audit")).json()["items"]
    released = next(e for e in audit if e["action"] == "slug.released")
    assert released["metadata"] == {"operator": True, "reason": "disputed address"}


async def test_manual_entitlements_raise_the_allowance_until_revoked(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, operator: httpx.AsyncClient
) -> None:
    owner, ws, _project, _build, _app = await ready_build(client, idp, github)
    before = (await owner.get(f"/api/v1/workspaces/{ws}/usage")).json()["allowance_micro_usd"]
    granted = await unsafe(
        operator,
        "POST",
        f"/api/v1/ops/workspaces/{ws}/entitlements",
        json={"allowance_micro_usd": 5_000_000, "days": 30, "reason": "closed beta"},
    )
    assert granted.status_code == 201, granted.text
    assert (await owner.get(f"/api/v1/workspaces/{ws}/usage")).json()[
        "allowance_micro_usd"
    ] == before + 5_000_000
    listed = (await operator.get(f"/api/v1/ops/workspaces/{ws}/entitlements")).json()["items"]
    assert [g["id"] for g in listed] == [granted.json()["id"]]
    assert (
        await unsafe(
            operator, "DELETE", f"/api/v1/ops/entitlements/{granted.json()['id']}", params={"reason": "ended"}
        )
    ).status_code == 204
    assert (await owner.get(f"/api/v1/workspaces/{ws}/usage")).json()["allowance_micro_usd"] == before
    actions = {e["action"] for e in (await owner.get(f"/api/v1/workspaces/{ws}/audit")).json()["items"]}
    assert {"entitlement.granted", "entitlement.revoked"} <= actions
    too_big = await unsafe(
        operator,
        "POST",
        f"/api/v1/ops/workspaces/{ws}/entitlements",
        json={"allowance_micro_usd": 5_000_000, "days": 400, "reason": "too long"},
    )
    assert too_big.status_code == 422


async def test_support_access_is_granted_by_admins_read_only_bounded_and_audited(
    client: httpx.AsyncClient,
    idp: FakeIdP,
    github: FakeGitHub,
    operator: httpx.AsyncClient,
    new_user: NewUser,
) -> None:
    owner, ws, project, build, _app = await ready_build(client, idp, github)
    reads = (
        f"/api/v1/workspaces/{ws}/projects",
        f"/api/v1/projects/{project}",
        f"/api/v1/builds/{build}/tree",
    )
    for path in reads:
        assert (await operator.get(path)).status_code == 404  # no grant: nothing to see
    editor = await new_user()
    await join(owner, ws, editor, "editor")
    url = f"/api/v1/workspaces/{ws}/support-access"
    assert (await unsafe(editor, "POST", url, json={"hours": 2, "reason": "help me"})).status_code == 403
    assert (await unsafe(owner, "POST", url, json={"hours": 73, "reason": "too long"})).status_code == 422
    grant = await unsafe(owner, "POST", url, json={"hours": 2, "reason": "build stuck at explain"})
    assert grant.status_code == 201 and grant.json()["active"] is True

    for path in reads:
        assert (await operator.get(path)).status_code == 200, path
    write = await unsafe(
        operator,
        "POST",
        f"/api/v1/projects/{project}/builds",
        json={},
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )
    assert write.status_code == 404  # support access never writes
    assert (
        await unsafe(operator, "POST", f"/api/v1/projects/{project}/deletion", json={"confirmation": "API"})
    ).status_code == 404
    accessed = ddl(
        "SELECT count(*) FROM audit_events WHERE workspace_id = :w AND action = 'support.accessed'", w=ws
    )[0][0]
    assert accessed == 1  # one event per reading session, not per request
    # A non-operator gains nothing from the grant.
    assert (await editor.get(f"/api/v1/workspaces/{ws}/support-access")).status_code == 403
    outsider = await new_user()
    assert (await outsider.get(reads[1])).status_code == 404

    ddl(
        "UPDATE support_grants SET expires_at = now() - interval '1 second', created_at = now() - interval '2 hours'"
        " WHERE id = :g",
        g=grant.json()["id"],
    )
    assert (await operator.get(reads[1])).status_code == 404  # expired
    second = (await unsafe(owner, "POST", url, json={"hours": 1, "reason": "one more look"})).json()
    assert (await operator.get(reads[1])).status_code == 200
    assert (await unsafe(owner, "DELETE", f"{url}/{second['id']}")).status_code == 204
    assert (await operator.get(reads[1])).status_code == 404  # revoked
    actions = [
        e["action"]
        for e in (await owner.get(f"/api/v1/workspaces/{ws}/audit", params={"action": "support."})).json()[
            "items"
        ]
    ]
    assert actions[0] == "support.revoked" and "support.granted" in actions and "support.accessed" in actions
