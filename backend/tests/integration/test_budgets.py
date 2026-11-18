"""Snapshots before builds, preflight estimates and single-envelope reservations (T10; 11, A05, A08).

Runs through the real API and runner with GitHub faked. The priced catalog is a synthetic test fixture,
not provider pricing.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.db import session_factory
from app.engine import catalog
from app.jobs import runner
from app.jobs.envelope import TaskEnvelope
from app.jobs.stages import Runtime
from tests.integration.auth_support import FakeIdP, ddl, unsafe
from tests.integration.conftest import RUNTIME_URL, needs_db
from tests.integration.test_builds import drive, setup_project
from tests.integration.test_github import FakeGitHub, github  # noqa: F401
from tests.unit.test_catalog_estimate import PRICED

pytestmark = [pytest.mark.integration, needs_db]

ALLOWANCE = 50_000_000  # USD 50 in micro-units


def configure(client: httpx.AsyncClient, *, allowance: int = ALLOWANCE, active_limit: int = 2) -> Any:
    app = client.app  # type: ignore[attr-defined]
    app.state.settings = app.state.settings.model_copy(
        update={
            "generation_allowance_micro_usd": allowance,
            "max_active_builds_per_workspace": active_limit,
            "anthropic_api_key": SecretStr("test-only"),  # the priced test model's provider is configured
        }
    )
    app.state.catalog = catalog.Catalog.model_validate(
        {
            "revision": PRICED["revision"],
            "models": [catalog.REPLAY_MODEL.model_dump(), *PRICED["models"]],  # type: ignore[list-item]
            "presets": [catalog.REPLAY_PRESET.model_dump(), *PRICED["presets"]],  # type: ignore[list-item]
        }
    )
    return app


async def drive_snapshot(app: Any, snapshot_id: str) -> list[str]:
    assert RUNTIME_URL
    rt = Runtime(
        sessions=session_factory(create_async_engine(RUNTIME_URL)),
        store=app.state.objects,
        github=app.state.github_app,
        allow_unsandboxed=True,
    )
    outcomes = []
    for (payload,) in ddl(
        "UPDATE outbox SET published_at = now() WHERE published_at IS NULL"
        " AND payload->>'snapshot_id' = :s RETURNING payload",
        s=snapshot_id,
    ):
        outcomes.append(await runner.process(rt, TaskEnvelope.model_validate(payload)))
    return outcomes


async def scanned_snapshot(owner: httpx.AsyncClient, app: Any, project: str) -> dict[str, Any]:
    requested = await unsafe(owner, "POST", f"/api/v1/projects/{project}/snapshots", json={})
    assert requested.status_code == 202, requested.text
    snapshot = requested.json()
    assert snapshot["status"] == "pending"
    assert await drive_snapshot(app, snapshot["id"]) == ["completed"]
    done = (await owner.get(f"/api/v1/projects/{project}/snapshots/{snapshot['id']}")).json()
    assert done["status"] == "scanned", done
    return dict(done)


async def make_estimate(
    owner: httpx.AsyncClient, project: str, snapshot: str, preset: str = "balanced-v1", **config: Any
) -> httpx.Response:
    return await unsafe(
        owner,
        "POST",
        f"/api/v1/projects/{project}/estimates",
        json={"snapshot_id": snapshot, "config": {"model_preset": preset, **config}},
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )


async def start_from(
    owner: httpx.AsyncClient, project: str, estimate: str, cap: int | None = None
) -> httpx.Response:
    body: dict[str, Any] = {"estimate_id": estimate}
    if cap is not None:
        body["cap_micro_usd"] = cap
    return await unsafe(
        owner,
        "POST",
        f"/api/v1/projects/{project}/builds",
        json=body,
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )


def account_counters(ws: str) -> tuple[int, int, int]:
    rows = ddl(
        "SELECT allowance_micro_usd, reserved_micro_usd, settled_micro_usd FROM usage_accounts WHERE workspace_id = :w",
        w=ws,
    )
    return tuple(rows[0])  # type: ignore[return-value]


def ledger_balance(ws: str) -> dict[str, int]:
    return {
        kind: int(total)
        for kind, total in ddl(
            "SELECT kind, sum(amount_micro_usd) FROM usage_entries WHERE workspace_id = :w GROUP BY kind",
            w=ws,
        )
    }


# ---------------------------------------------------------------- snapshots
async def test_snapshot_is_acquired_and_scanned_before_any_build(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, project = await setup_project(client, idp, github)
    app = configure(client)
    snapshot = await scanned_snapshot(owner, app, project)
    assert snapshot["commit_sha"] == github.commit_sha
    assert snapshot["scan_summary"] == {"blocked": 1, "excluded": 2, "selected": 3, "unsupported": 1}
    assert ddl("SELECT count(*) FROM builds WHERE project_id = :p", p=project)[0][0] == 0
    # A second request for the same commit reuses the immutable snapshot and schedules nothing.
    again = await unsafe(owner, "POST", f"/api/v1/projects/{project}/snapshots", json={})
    assert again.json()["id"] == snapshot["id"] and again.json()["status"] == "scanned"
    assert await drive_snapshot(app, snapshot["id"]) == []

    first = await owner.get(
        f"/api/v1/projects/{project}/snapshots/{snapshot['id']}/files", params={"limit": 4}
    )
    page = first.json()
    assert [f["path"] for f in page["items"]] == [".env", "README.md", "deploy/key.pem", "src/app.py"]
    rest = (
        await owner.get(
            f"/api/v1/projects/{project}/snapshots/{snapshot['id']}/files",
            params={"limit": 4, "cursor": page["next_cursor"]},
        )
    ).json()
    assert [f["path"] for f in rest["items"]] == ["src/settings.py", "src/util.py", "web/index.ts"]
    assert rest["next_cursor"] is None
    selected = (
        await owner.get(
            f"/api/v1/projects/{project}/snapshots/{snapshot['id']}/files", params={"status": "selected"}
        )
    ).json()
    assert {f["path"] for f in selected["items"]} == {"src/app.py", "src/util.py", "web/index.ts"}
    replayed = await owner.get(  # a cursor is bound to its filter
        f"/api/v1/projects/{project}/snapshots/{snapshot['id']}/files",
        params={"status": "selected", "cursor": page["next_cursor"]},
    )
    assert replayed.status_code == 400 and replayed.json()["error"]["code"] == "INVALID_CURSOR"


# ---------------------------------------------------------------- estimates
async def test_estimate_is_immutable_scoped_and_expires(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, project = await setup_project(client, idp, github)
    app = configure(client)
    requested = await unsafe(owner, "POST", f"/api/v1/projects/{project}/snapshots", json={})
    pending = await make_estimate(owner, project, requested.json()["id"])
    assert pending.status_code == 409 and pending.json()["error"]["code"] == "SNAPSHOT_NOT_READY"
    await drive_snapshot(app, requested.json()["id"])

    created = await make_estimate(owner, project, requested.json()["id"], include=["src/**"])
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["commit_sha"] == github.commit_sha and body["price_version"] == "catalog-2026-11-b"
    assert (body["selected_files"], body["excluded_files"]) == (2, 5)
    assert 0 < body["cost_micro_usd"]["low"] < body["cost_micro_usd"]["high"]
    assert body["expired"] is False and "actual usage is metered" in body["disclaimer"]
    fetched = await owner.get(f"/api/v1/projects/{project}/estimates/{body['id']}")
    assert fetched.json()["config_digest"] == body["config_digest"]

    free = await make_estimate(owner, project, requested.json()["id"], preset="replay-v2")
    assert free.json()["cost_micro_usd"] == {"low": 0, "high": 0}
    unknown = await make_estimate(owner, project, requested.json()["id"], preset="fast-v9")
    assert unknown.status_code == 422 and unknown.json()["error"]["code"] == "PRESET_UNKNOWN"
    empty = await make_estimate(owner, project, requested.json()["id"], include=["docs/**"])
    assert empty.status_code == 422 and empty.json()["error"]["code"] == "SCOPE_EMPTY"
    invalid = await make_estimate(owner, project, requested.json()["id"], languages=["de"])
    assert invalid.status_code == 422

    ddl(
        "UPDATE estimates SET created_at = now() - interval '2 hours', expires_at = now() - interval '1 hour'"
        " WHERE id = :e",
        e=body["id"],
    )
    expired = await start_from(owner, project, body["id"])
    assert expired.status_code == 409 and expired.json()["error"]["code"] == "ESTIMATE_EXPIRED"


async def test_estimate_goes_stale_when_the_catalog_changes(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, project = await setup_project(client, idp, github)
    app = configure(client)
    snapshot = await scanned_snapshot(owner, app, project)
    estimate = (await make_estimate(owner, project, snapshot["id"])).json()
    app.state.catalog = app.state.catalog.model_copy(update={"revision": "catalog-2026-12-a"})
    stale = await start_from(owner, project, estimate["id"])
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "ESTIMATE_STALE"


# ---------------------------------------------------------------- reservations
async def test_build_reserves_its_cap_once_and_releases_the_rest_at_the_end(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, ws, project = await setup_project(client, idp, github)
    app = configure(client)
    snapshot = await scanned_snapshot(owner, app, project)
    estimate = (await make_estimate(owner, project, snapshot["id"], preset="replay-v2")).json()
    github.commit_sha = "d" * 40  # the branch moves after the estimate (A05)

    started = await start_from(owner, project, estimate["id"], cap=2_000_000)
    assert started.status_code == 202, started.text
    build = started.json()
    assert build["commit_sha"] == snapshot["commit_sha"] and build["snapshot_id"] == snapshot["id"]
    assert build["usage"] == {
        "cap_micro_usd": 2_000_000,
        "reserved_micro_usd": 2_000_000,
        "settled_micro_usd": 0,
    }
    assert account_counters(ws) == (ALLOWANCE, 2_000_000, 0)
    usage = (await owner.get(f"/api/v1/workspaces/{ws}/usage")).json()
    assert usage["available_micro_usd"] == ALLOWANCE - 2_000_000

    outcomes = await drive(app, build["id"])
    assert outcomes == ["completed"] * 8, outcomes  # acquire skipped: the snapshot was already scanned
    done = (await owner.get(f"/api/v1/builds/{build['id']}")).json()
    assert done["status"] == "ready"
    assert done["usage"] == {"cap_micro_usd": 2_000_000, "reserved_micro_usd": 0, "settled_micro_usd": 0}
    assert account_counters(ws) == (ALLOWANCE, 0, 0)
    assert ledger_balance(ws) == {"reserve": 2_000_000, "release": 2_000_000}
    entries = (await owner.get(f"/api/v1/workspaces/{ws}/usage/entries", params={"limit": 1})).json()
    assert entries["items"][0]["kind"] == "release" and entries["next_cursor"]
    older = (
        await owner.get(
            f"/api/v1/workspaces/{ws}/usage/entries", params={"limit": 1, "cursor": entries["next_cursor"]}
        )
    ).json()
    assert older["items"][0]["kind"] == "reserve" and older["next_cursor"] is None

    # The default cap is the lower of the remaining allowance and the estimate's high bound.
    estimate = (await make_estimate(owner, project, snapshot["id"])).json()
    default = (await start_from(owner, project, estimate["id"])).json()
    assert default["usage"]["cap_micro_usd"] == estimate["cost_micro_usd"]["high"]


async def test_cap_cannot_exceed_the_remaining_allowance(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, ws, project = await setup_project(client, idp, github)
    app = configure(client, allowance=1_000_000)
    snapshot = await scanned_snapshot(owner, app, project)
    estimate = (await make_estimate(owner, project, snapshot["id"])).json()
    refused = await start_from(owner, project, estimate["id"], cap=1_000_001)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "ALLOWANCE_EXCEEDED"
    assert refused.json()["error"]["details"]["available_micro_usd"] == 1_000_000
    assert ledger_balance(ws) == {}
    usage = (await owner.get(f"/api/v1/workspaces/{ws}/usage")).json()
    assert (usage["reserved_micro_usd"], usage["available_micro_usd"]) == (0, 1_000_000)
    assert ddl("SELECT count(*) FROM builds WHERE project_id = :p", p=project)[0][0] == 0


async def create_second_project(owner: httpx.AsyncClient, ws: str, project: str) -> str:
    repository = ddl("SELECT repository_id FROM projects WHERE id = :p", p=project)[0][0]
    created = await unsafe(
        owner,
        "POST",
        f"/api/v1/workspaces/{ws}/projects",
        json={"repository_id": str(repository), "name": "Second", "slug": f"second-{secrets.token_hex(3)}"},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def test_racing_reservations_cannot_exceed_the_allowance(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, ws, project = await setup_project(client, idp, github)
    app = configure(client, allowance=3_000_000)
    second = await create_second_project(owner, ws, project)
    estimates = []
    for p in (project, second):
        snapshot = await scanned_snapshot(owner, app, p)
        estimates.append((await make_estimate(owner, p, snapshot["id"])).json()["id"])

    # The account exists before the race, so only its row lock (not the insert) can serialize the starts.
    assert (await owner.get(f"/api/v1/workspaces/{ws}/usage")).json()["available_micro_usd"] == 3_000_000
    results = await asyncio.gather(
        start_from(owner, project, estimates[0], cap=2_000_000),
        start_from(owner, second, estimates[1], cap=2_000_000),
    )
    codes = sorted(r.status_code for r in results)
    assert codes == [202, 409], [r.text for r in results]
    refused = next(r for r in results if r.status_code == 409)
    assert refused.json()["error"]["code"] == "ALLOWANCE_EXCEEDED"
    assert account_counters(ws) == (3_000_000, 2_000_000, 0)
    assert ledger_balance(ws) == {"reserve": 2_000_000}


async def test_workspace_active_build_limit(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, ws, project = await setup_project(client, idp, github)
    app = configure(client, active_limit=1)
    second = await create_second_project(owner, ws, project)
    snapshot = await scanned_snapshot(owner, app, project)
    first = await start_from(
        owner, project, (await make_estimate(owner, project, snapshot["id"])).json()["id"]
    )
    assert first.status_code == 202
    snapshot2 = await scanned_snapshot(owner, app, second)
    blocked = await start_from(
        owner, second, (await make_estimate(owner, second, snapshot2["id"])).json()["id"]
    )
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "WORKSPACE_BUILD_LIMIT"
    # Replay builds count against the same limit.
    replay = await unsafe(
        owner,
        "POST",
        f"/api/v1/projects/{second}/builds",
        json={},
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )
    assert replay.status_code == 409 and replay.json()["error"]["code"] == "WORKSPACE_BUILD_LIMIT"


async def test_other_tenants_cannot_see_estimates_or_usage(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, ws, project = await setup_project(client, idp, github)
    app = configure(client)
    snapshot = await scanned_snapshot(owner, app, project)
    estimate = (await make_estimate(owner, project, snapshot["id"])).json()
    stranger, _, _ = await setup_project(client, idp, github)
    for path in (
        f"/api/v1/projects/{project}/estimates/{estimate['id']}",
        f"/api/v1/projects/{project}/snapshots/{snapshot['id']}",
        f"/api/v1/projects/{project}/snapshots/{snapshot['id']}/files",
        f"/api/v1/workspaces/{ws}/usage",
        f"/api/v1/workspaces/{ws}/usage/entries",
    ):
        response = await stranger.get(path)
        assert response.status_code == 404, (path, response.text)
    stolen = await start_from(stranger, project, estimate["id"])
    assert stolen.status_code == 404
    presets = (await owner.get("/api/v1/catalog/presets")).json()
    assert presets["price_version"] == "catalog-2026-11-b"
    assert [p["name"] for p in presets["items"]] == ["replay-v2", "balanced-v1"]


async def test_account_lock_serializes_reservations(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    """Two transactions: the second account read waits for the first reservation to commit, so it sees
    the reduced availability. Without the row lock both would see the full allowance (A08)."""
    from uuid import UUID

    from app.core.tenancy import set_context
    from app.modules.usage import ledger

    _owner, ws, _project = await setup_project(client, idp, github)
    workspace = UUID(ws)
    assert RUNTIME_URL
    engine = create_async_engine(RUNTIME_URL)
    sessions = session_factory(engine)
    try:
        async with sessions() as first, sessions() as second:
            await set_context(first, workspace_id=workspace)
            await ledger.current_account(first, workspace, 3_000_000, lock=True)
            await first.commit()  # the account exists before the race
            await set_context(first, workspace_id=workspace)
            held = await ledger.current_account(first, workspace, 3_000_000, lock=True)
            await set_context(second, workspace_id=workspace)
            waiting = asyncio.create_task(ledger.current_account(second, workspace, 3_000_000, lock=True))
            await asyncio.sleep(0.5)
            assert not waiting.done(), "the second reservation must wait for the account lock"
            await first.execute(
                sa_text(
                    "UPDATE usage_accounts SET reserved_micro_usd = 2000000 WHERE workspace_id = :w AND id = :a"
                ),
                {"w": workspace, "a": held.id},
            )
            await first.commit()
            seen = await asyncio.wait_for(waiting, 5)
            assert seen.available_micro_usd == 1_000_000
            await second.rollback()
    finally:
        await engine.dispose()


async def test_workspace_snapshot_acquisition_limit(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, ws, project = await setup_project(client, idp, github)
    app = client.app  # type: ignore[attr-defined]
    app.state.settings = app.state.settings.model_copy(update={"max_active_acquisitions_per_workspace": 1})
    second = await create_second_project(owner, ws, project)
    first = await unsafe(owner, "POST", f"/api/v1/projects/{project}/snapshots", json={})
    assert first.status_code == 202
    again = await unsafe(owner, "POST", f"/api/v1/projects/{project}/snapshots", json={})
    assert again.status_code == 202  # the same pending snapshot does not count against itself
    blocked = await unsafe(owner, "POST", f"/api/v1/projects/{second}/snapshots", json={})
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "ACQUISITION_LIMIT"
    await drive_snapshot(app, first.json()["id"])
    allowed = await unsafe(owner, "POST", f"/api/v1/projects/{second}/snapshots", json={})
    assert allowed.status_code == 202, allowed.text


async def test_presets_need_a_configured_provider(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, project = await setup_project(client, idp, github)
    app = configure(client)
    snapshot = await scanned_snapshot(owner, app, project)
    app.state.settings = app.state.settings.model_copy(update={"anthropic_api_key": None})
    refused = await make_estimate(owner, project, snapshot["id"])
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "PRESET_UNAVAILABLE"
    listed = (await owner.get("/api/v1/catalog/presets")).json()
    assert [p["name"] for p in listed["items"]] == ["replay-v2"]
