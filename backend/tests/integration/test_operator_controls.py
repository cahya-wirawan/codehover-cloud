"""Operator controls and member spending limits (0044; 02 S21, S16; 09; 11; 22).

Operators list people and owners (handles, IDs, states, counts and money, never names or emails), find a
person by an exact email, suspend and reinstate people, and credit an owner's spending this month. Workspace
owners and admins give members monthly spending limits, which build admission enforces.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.modules.usage.ledger import period_bounds
from tests.integration.auth_support import FakeIdP, browser, ddl, sign_in, unsafe
from tests.integration.conftest import needs_db
from tests.integration.test_budgets import (
    configure,
    create_second_project,
    make_estimate,
    scanned_snapshot,
    start_from,
)
from tests.integration.test_builds import setup_project
from tests.integration.test_github import FakeGitHub, github  # noqa: F401
from tests.integration.test_mcp import create_token
from tests.integration.test_operations import operator  # noqa: F401
from tests.integration.test_publication import address, publish, ready_build
from tests.integration.test_workspaces import NewUser, code, join, new_user  # noqa: F401

pytestmark = [pytest.mark.integration, needs_db]


async def me(person: httpx.AsyncClient) -> dict[str, str]:
    body: dict[str, str] = (await person.get("/api/v1/me")).json()
    return body


def settle(ws: str, amount: int) -> None:
    """Stand in for this month's settled spending (the ledger's own paths are tested in test_budgets)."""
    start, end = period_bounds(datetime.now(UTC))
    ddl(
        "INSERT INTO usage_accounts(workspace_id, period_start, period_end, allowance_micro_usd, settled_micro_usd)"
        " VALUES (:w, :s, :e, :n, :n) ON CONFLICT (workspace_id, period_start)"
        " DO UPDATE SET settled_micro_usd = :n, allowance_micro_usd = GREATEST(usage_accounts.allowance_micro_usd, :n)",
        w=ws,
        s=start,
        e=end,
        n=amount,
    )


# ---------------------------------------------------------------- people and owners
async def test_operator_lists_show_handles_and_money_never_names_or_emails(
    client: httpx.AsyncClient,
    idp: FakeIdP,
    github: FakeGitHub,
    operator: httpx.AsyncClient,
    new_user: NewUser,
) -> None:
    owner, ws, _project, _build, _app = await ready_build(client, idp, github)
    profile = await me(owner)
    handle = profile["handle"]
    other = await new_user()
    other_profile = await me(other)
    email = other.email  # type: ignore[attr-defined]

    # Everyone else: as if the routes did not exist.
    for path in ("/api/v1/ops/people", f"/api/v1/ops/people/{profile['id']}", "/api/v1/ops/owners"):
        assert (await owner.get(path)).status_code == 404
    for method, path, body in (
        ("POST", "/api/v1/ops/people/lookup", {"email": email}),
        ("POST", f"/api/v1/ops/people/{profile['id']}/suspension", {"reason": "nope"}),
        ("POST", f"/api/v1/ops/owners/{ws}/credit", {"reason": "nope"}),
    ):
        assert (await unsafe(owner, method, path, json=body)).status_code == 404

    listed = await operator.get("/api/v1/ops/people", params={"handle": handle})
    assert listed.status_code == 200
    row = next(p for p in listed.json()["items"] if p["user_id"] == profile["id"])
    assert row["handle"] == handle and row["status"] == "active" and row["workspaces"] >= 1
    assert row["last_active_at"] is not None and row["operator"] is False
    found = await unsafe(operator, "POST", "/api/v1/ops/people/lookup", json={"email": email.upper()})
    assert [p["user_id"] for p in found.json()["items"]] == [other_profile["id"]]
    missing = await unsafe(operator, "POST", "/api/v1/ops/people/lookup", json={"email": "no@example.test"})
    assert missing.json()["items"] == []
    detail = await operator.get(f"/api/v1/ops/people/{profile['id']}")
    assert detail.status_code == 200
    data = detail.json()
    assert [w["workspace_id"] for w in data["workspaces"]] == [ws]
    assert data["workspaces"][0]["role"] == "owner" and data["sign_in"] == ["github"]
    assert data["live_sessions"] >= 1

    owners = await operator.get("/api/v1/ops/owners", params={"handle": handle})
    mine = next(o for o in owners.json()["items"] if o["handle"] == handle)
    assert mine["kind"] == "user" and mine["workspaces"] == 1 and mine["projects"] == 1
    assert mine["allowance_micro_usd"] >= 0 and mine["plan_key"] is None

    for response in (listed, found, detail, owners):
        for private in (email, profile["display_name"], other_profile["display_name"]):
            assert private not in response.text
    paged = (await operator.get("/api/v1/ops/people", params={"limit": 1})).json()
    assert len(paged["items"]) == 1 and paged["next_cursor"]
    second = await operator.get("/api/v1/ops/people", params={"limit": 1, "cursor": paged["next_cursor"]})
    assert second.status_code == 200 and second.json()["items"][0]["user_id"] != paged["items"][0]["user_id"]


# ---------------------------------------------------------------- suspension
async def test_suspension_ends_every_way_in_cancels_builds_and_keeps_sites_up(
    client: httpx.AsyncClient,
    idp: FakeIdP,
    github: FakeGitHub,
    operator: httpx.AsyncClient,
) -> None:
    owner, ws, project, build, app = await ready_build(client, idp, github)
    profile = await me(owner)
    ((owner_sub,),) = ddl(
        "SELECT subject FROM oauth_identities WHERE user_id = :u AND provider = 'github'", u=profile["id"]
    )
    site = await address(owner, project)
    published = await publish(owner, project, build_id=build, visibility="public", confirmation="API")
    assert published.status_code == 200, published.text
    token = await create_token(owner, ws)
    assert token.status_code == 201, token.text
    running = await unsafe(
        owner,
        "POST",
        f"/api/v1/projects/{project}/builds",
        json={},
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )
    assert running.status_code == 202, running.text

    myself = (await me(operator))["id"]
    refused = await unsafe(
        operator, "POST", f"/api/v1/ops/people/{myself}/suspension", json={"reason": "oops"}
    )
    assert refused.status_code == 409 and code(refused) == "OPERATOR_TARGET"
    short = await unsafe(
        operator, "POST", f"/api/v1/ops/people/{profile['id']}/suspension", json={"reason": "x"}
    )
    assert short.status_code == 422

    suspended = await unsafe(
        operator, "POST", f"/api/v1/ops/people/{profile['id']}/suspension", json={"reason": "abuse report"}
    )
    assert suspended.status_code == 200, suspended.text
    body = suspended.json()
    assert body["status"] == "disabled" and body["disabled_at"] and body["cancelled_builds"] == 1
    assert body["live_sessions"] == 0 and body["live_tokens"] == 0

    assert (await owner.get("/api/v1/me")).status_code == 401  # the session ended
    assert ddl(
        "SELECT count(*) FROM access_tokens WHERE user_id = :u AND revoked_at IS NULL", u=profile["id"]
    ) == [(0,)]
    (cancel_requested,) = ddl(
        "SELECT cancel_requested_at IS NOT NULL FROM builds WHERE id = :b", b=running.json()["id"]
    )[0]
    assert cancel_requested
    assert (await client.get(f"/api/v1/published/{site}")).status_code == 200  # the site stays up

    # Signing in again is refused, with a reason the login page can show.
    async with browser(app) as again:
        idp.github_id = int(owner_sub)
        callback = await sign_in(again, idp, provider="github")
        assert callback.status_code in (302, 303)
        assert parse_qs(urlsplit(callback.headers["location"]).query)["error"] == ["suspended"]

    repeated = await unsafe(
        operator, "POST", f"/api/v1/ops/people/{profile['id']}/suspension", json={"reason": "abuse report"}
    )
    assert repeated.status_code == 200 and repeated.json()["cancelled_builds"] == 0
    events = (await operator.get("/api/v1/ops/events")).json()["items"]
    assert [e["action"] for e in events if e["target_id"] == profile["id"]] == ["person.suspended"]
    audit = ddl(
        "SELECT action, metadata->>'reason' FROM audit_events WHERE workspace_id = :w"
        " AND action IN ('member.suspended', 'member.reinstated', 'build.cancel_requested') ORDER BY created_at",
        w=ws,
    )
    assert [tuple(r) for r in audit] == [
        ("member.suspended", "abuse report"),
        ("build.cancel_requested", "account_suspended"),
    ]

    reinstated = await unsafe(
        operator,
        "DELETE",
        f"/api/v1/ops/people/{profile['id']}/suspension",
        params={"reason": "appeal upheld"},
    )
    assert reinstated.status_code == 204
    async with browser(app) as back:
        idp.github_id = int(owner_sub)
        assert "error" not in (await sign_in(back, idp, provider="github")).headers["location"]
        assert (await back.get("/api/v1/me")).json()["id"] == profile["id"]
    # Tokens ended by the suspension stay ended.
    assert ddl(
        "SELECT count(*) FROM access_tokens WHERE user_id = :u AND revoked_at IS NULL", u=profile["id"]
    ) == [(0,)]
    gone = await unsafe(operator, "POST", f"/api/v1/ops/people/{ws}/suspension", json={"reason": "typo id"})
    assert gone.status_code == 404


# ---------------------------------------------------------------- credits
async def test_a_credit_hands_back_this_months_spending_once(
    client: httpx.AsyncClient,
    idp: FakeIdP,
    github: FakeGitHub,
    operator: httpx.AsyncClient,
) -> None:
    owner, ws, _project = await setup_project(client, idp, github)
    configure(client)
    profile = await me(owner)
    owner_row = (await operator.get("/api/v1/ops/owners", params={"handle": profile["handle"]})).json()[
        "items"
    ]
    namespace = next(o for o in owner_row if o["handle"] == profile["handle"])["namespace_id"]
    nothing = await unsafe(
        operator, "POST", f"/api/v1/ops/owners/{namespace}/credit", json={"reason": "reset"}
    )
    assert nothing.status_code == 409 and code(nothing) == "NOTHING_TO_CREDIT"

    settle(ws, 3_000_000)
    before = (await owner.get(f"/api/v1/workspaces/{ws}/usage")).json()
    credited = await unsafe(
        operator, "POST", f"/api/v1/ops/owners/{namespace}/credit", json={"reason": "test builds by us"}
    )
    assert credited.status_code == 201, credited.text
    grant = credited.json()
    _start, end = period_bounds(datetime.now(UTC))
    assert grant["kind"] == "credit" and grant["allowance_micro_usd"] == 3_000_000
    assert grant["workspace_id"] == ws and datetime.fromisoformat(grant["expires_at"]) == end
    after = (await owner.get(f"/api/v1/workspaces/{ws}/usage")).json()
    assert after["allowance_micro_usd"] == before["allowance_micro_usd"] + 3_000_000
    assert after["settled_micro_usd"] == 3_000_000  # the ledger keeps the real spending

    again = await unsafe(operator, "POST", f"/api/v1/ops/owners/{namespace}/credit", json={"reason": "twice"})
    assert again.status_code == 409 and code(again) == "NOTHING_TO_CREDIT"
    settle(ws, 4_000_000)
    more = await unsafe(
        operator, "POST", f"/api/v1/ops/owners/{namespace}/credit", json={"reason": "the rest"}
    )
    assert more.status_code == 201 and more.json()["allowance_micro_usd"] == 1_000_000
    listed = (await operator.get(f"/api/v1/ops/workspaces/{ws}/entitlements")).json()["items"]
    assert sorted(g["allowance_micro_usd"] for g in listed if g["kind"] == "credit") == [1_000_000, 3_000_000]
    audit = (await owner.get(f"/api/v1/workspaces/{ws}/audit")).json()["items"]
    assert sum(e["action"] == "entitlement.credited" for e in audit) == 2
    missing = await unsafe(operator, "POST", f"/api/v1/ops/owners/{ws}/credit", json={"reason": "typo id"})
    assert missing.status_code == 404


# ---------------------------------------------------------------- member limits
async def test_member_limits_cap_what_a_member_can_start(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, new_user: NewUser
) -> None:
    owner, ws, project = await setup_project(client, idp, github)
    app = configure(client)
    editor, admin = await new_user(), await new_user()
    await join(owner, ws, editor, "editor")
    await join(owner, ws, admin, "admin")
    editor_id, owner_id = (await me(editor))["id"], (await me(owner))["id"]
    limit_url = f"/api/v1/workspaces/{ws}/members/{editor_id}/spending-limit"

    assert (await unsafe(editor, "PUT", limit_url, json={"monthly_limit_micro_usd": 5})).status_code == 403
    over_owner = await unsafe(
        admin,
        "PUT",
        f"/api/v1/workspaces/{ws}/members/{owner_id}/spending-limit",
        json={"monthly_limit_micro_usd": 5},
    )
    assert over_owner.status_code == 403  # admins limit editors and viewers only, like role changes
    assert (await unsafe(admin, "PUT", limit_url, json={"monthly_limit_micro_usd": -1})).status_code == 422
    assert (await unsafe(admin, "PUT", limit_url, json={"monthly_limit_micro_usd": 1_000})).status_code == 204

    snapshot = await scanned_snapshot(owner, app, project)
    estimate = (await make_estimate(editor, project, snapshot["id"])).json()
    assert estimate["cost_micro_usd"]["high"] > 1_000
    too_big = await start_from(editor, project, estimate["id"], cap=5_000)
    assert too_big.status_code == 409 and code(too_big) == "MEMBER_LIMIT_EXCEEDED"
    assert too_big.json()["error"]["details"] == {"available_micro_usd": 1_000, "cap_micro_usd": 5_000}
    first = await start_from(editor, project, estimate["id"])
    assert first.status_code == 202, first.text
    assert first.json()["usage"]["cap_micro_usd"] == 1_000  # the default cap stops at the limit

    usage = (await editor.get(f"/api/v1/workspaces/{ws}/usage")).json()
    assert (usage["member_limit_micro_usd"], usage["member_used_micro_usd"]) == (1_000, 1_000)
    second_project = await create_second_project(owner, ws, project)
    snapshot2 = await scanned_snapshot(owner, app, second_project)
    estimate2 = (await make_estimate(editor, second_project, snapshot2["id"])).json()
    used_up = await start_from(editor, second_project, estimate2["id"])
    assert used_up.status_code == 409 and code(used_up) == "MEMBER_LIMIT_EXCEEDED"
    assert (
        used_up.json()["error"]["message"] == "Your spending limit in this workspace is used up this month."
    )
    # Others in the workspace are not held back by the editor's limit.
    assert (await start_from(owner, second_project, estimate2["id"])).status_code == 202

    assert (await editor.get(f"/api/v1/workspaces/{ws}/usage/members")).status_code == 403
    members = (await admin.get(f"/api/v1/workspaces/{ws}/usage/members")).json()["items"]
    by_id = {m["user_id"]: m for m in members}
    assert by_id[editor_id]["limit_micro_usd"] == 1_000 and by_id[editor_id]["builds"] == 1
    assert by_id[editor_id]["reserved_micro_usd"] == 1_000 and by_id[owner_id]["limit_micro_usd"] is None
    assert by_id[owner_id]["builds"] == 1

    assert (await unsafe(admin, "PUT", limit_url, json={"monthly_limit_micro_usd": None})).status_code == 204
    assert (await editor.get(f"/api/v1/workspaces/{ws}/usage")).json()["member_limit_micro_usd"] is None
    audit = ddl(
        "SELECT metadata->>'from', metadata->>'to' FROM audit_events WHERE workspace_id = :w"
        " AND action = 'member.spending_limit_changed' ORDER BY created_at",
        w=ws,
    )
    assert [tuple(r) for r in audit] == [(None, "1000"), ("1000", None)]

    # A limit ends with the membership.
    assert (await unsafe(admin, "PUT", limit_url, json={"monthly_limit_micro_usd": 7})).status_code == 204
    assert (await unsafe(owner, "DELETE", f"/api/v1/workspaces/{ws}/members/{editor_id}")).status_code == 204
    assert ddl("SELECT count(*) FROM member_limits WHERE workspace_id = :w", w=ws) == [(0,)]
    gone = await unsafe(admin, "PUT", limit_url, json={"monthly_limit_micro_usd": 7})
    assert gone.status_code == 404
