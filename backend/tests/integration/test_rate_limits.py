"""Rate limits shared by every replica (06 "Initial rate policies"; 0019).

Limits are lowered per test; client addresses are made unique through a trusted proxy's X-Forwarded-For,
so counters of other tests never interfere. Windows are fixed, so tests avoid the last seconds of one.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any

import httpx
import pytest

from tests.integration.auth_support import FakeIdP, browser, ddl, unsafe
from tests.integration.conftest import needs_db
from tests.integration.test_budgets import make_estimate
from tests.integration.test_builds import setup_project
from tests.integration.test_github import FakeGitHub, github  # noqa: F401
from tests.integration.test_publication import publish, ready_build
from tests.integration.test_workspaces import NewUser, new_user  # noqa: F401

pytestmark = [pytest.mark.integration, needs_db]


def limits(app: Any, **values: Any) -> None:
    app.state.settings = app.state.settings.model_copy(update={"trusted_proxies": "127.0.0.0/8", **values})


async def fresh_window(seconds: int = 60) -> None:
    remaining = seconds - time.time() % seconds
    if remaining < 5:  # too close to the window's end: start in the next one
        await asyncio.sleep(remaining + 0.1)


def address() -> str:
    return (
        f"203.0.113.{secrets.randbelow(250) + 1}"
        if secrets.randbelow(2)
        else f"2001:db8:{secrets.token_hex(2)}:{secrets.token_hex(2)}::1"  # distinct /64s
    )


async def test_each_user_has_a_request_budget(client: httpx.AsyncClient, new_user: NewUser) -> None:
    first, second = await new_user(), await new_user()
    limits(client.app, rate_limit_user_per_minute=5)  # type: ignore[attr-defined]
    await fresh_window()
    statuses = [(await first.get("/api/v1/workspaces")).status_code for _ in range(6)]
    assert statuses[:5] == [200] * 5 and statuses[5] == 429
    limited = await first.get("/api/v1/workspaces")
    body = limited.json()["error"]
    assert body["code"] == "RATE_LIMITED" and body["retryable"] is True
    assert 1 <= int(limited.headers["retry-after"]) <= 60
    assert body["details"]["retry_after_seconds"] == int(limited.headers["retry-after"])
    assert (await second.get("/api/v1/workspaces")).status_code == 200  # someone else's budget


async def test_sign_in_starts_are_limited_per_client_address(client: httpx.AsyncClient, idp: FakeIdP) -> None:
    app = client.app  # type: ignore[attr-defined]
    limits(app, rate_limit_signin_per_minute=3)
    await fresh_window()
    mine, other = address(), address()
    async with browser(app) as person:
        starts = [
            await person.get("/api/v1/auth/google/start", headers={"X-Forwarded-For": mine}) for _ in range(4)
        ]
        assert all(s.status_code == 302 for s in starts)
        assert all("accounts.google.com" in s.headers["location"] for s in starts[:3])
        assert starts[3].headers["location"] == "/login?error=rate_limited"  # a page, not JSON
        elsewhere = await person.get("/api/v1/auth/google/start", headers={"X-Forwarded-For": other})
        assert "accounts.google.com" in elsewhere.headers["location"]
    stored = {r[0] for r in ddl("SELECT bucket FROM rate_limits")}
    assert not any(mine in b or other in b for b in stored)  # HMACs only


async def test_public_delivery_is_limited_per_client_address(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, project, build, app = await ready_build(client, idp, github)
    slug = f"busy-{secrets.token_hex(3)}"
    assert (
        await publish(
            owner, project, build_id=build, visibility="public", public_slug=slug, confirmation="API"
        )
    ).status_code == 200
    limits(app, rate_limit_public_per_minute=3)
    await fresh_window()
    reader, other = address(), address()
    async with browser(app) as visitor:
        codes = [
            (await visitor.get(f"/api/v1/published/{slug}", headers={"X-Forwarded-For": reader})).status_code
            for _ in range(4)
        ]
        assert codes == [200, 200, 200, 429]
        assert (
            await visitor.get(f"/api/v1/published/{slug}", headers={"X-Forwarded-For": other})
        ).status_code == 200


async def test_workspace_quotas_count_new_work_not_idempotent_replays(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, project = await setup_project(client, idp, github)
    app = client.app  # type: ignore[attr-defined]
    limits(
        app, rate_limit_builds_per_day=2, rate_limit_estimates_per_hour=1, rate_limit_acquisitions_per_hour=1
    )
    key = secrets.token_hex(12)

    async def start(idempotency: str) -> httpx.Response:
        return await unsafe(
            owner,
            "POST",
            f"/api/v1/projects/{project}/builds",
            json={},
            headers={"Idempotency-Key": idempotency},
        )

    first = await start(key)
    assert first.status_code == 202
    assert (await start(key)).status_code == 202  # the replay returns the original and costs nothing
    second = await start(secrets.token_hex(12))
    assert second.status_code in (202, 409)  # counted (409 only if the workspace's active-build limit hit)
    third = await start(secrets.token_hex(12))
    assert third.status_code == 429 and third.json()["error"]["code"] == "RATE_LIMITED"

    snapshot = ddl("SELECT id FROM snapshots WHERE project_id = :p LIMIT 1", p=project)[0][0]
    # Attempts count, whatever their outcome (this snapshot is not scanned yet, so 409).
    assert (await make_estimate(owner, project, str(snapshot), preset="replay-v2")).status_code != 429
    assert (await make_estimate(owner, project, str(snapshot), preset="replay-v2")).status_code == 429
    snap = lambda: unsafe(owner, "POST", f"/api/v1/projects/{project}/snapshots", json={})  # noqa: E731
    assert (await snap()).status_code in (200, 202)
    assert (await snap()).status_code == 429
