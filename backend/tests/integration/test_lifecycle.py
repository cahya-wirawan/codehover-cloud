"""Cancellation, retry builds and progress events (06; A10, A13).

A scripted provider observes or triggers cancellation mid-call; no real provider is called.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from app.engine.gateway import GenerationRequest, GenerationResult, ProviderError
from tests.integration.auth_support import FakeIdP, ddl, unsafe
from tests.integration.conftest import needs_db
from tests.integration.test_budgets import account_counters, ledger_balance, make_estimate, scanned_snapshot
from tests.integration.test_builds import drive, setup_project
from tests.integration.test_generation import ScriptedProvider, attempts, generation_build, runtime_for
from tests.integration.test_github import FakeGitHub, github  # noqa: F401

pytestmark = [pytest.mark.integration, needs_db]


async def cancel(owner: httpx.AsyncClient, build: str) -> httpx.Response:
    return await unsafe(owner, "POST", f"/api/v1/builds/{build}/cancel", json={"reason": "test"})


async def retry(owner: httpx.AsyncClient, build: str, **body: Any) -> httpx.Response:
    return await unsafe(
        owner,
        "POST",
        f"/api/v1/builds/{build}/retry",
        json=body,
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )


async def test_cancelling_a_queued_build_stops_it_now_and_releases_the_envelope(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, ws, build, app = await generation_build(client, idp, github, preset="balanced-v1", cap=2_000_000)
    cancelled = await cancel(owner, build)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["cancel_requested_at"] is not None
    assert (await cancel(owner, build)).json()["status"] == "cancelled"  # idempotent
    provider = ScriptedProvider()
    assert await drive(app, build, runtime=runtime_for(app, provider)) == ["not_runnable"]
    assert provider.calls == [] and attempts(build) == []
    assert {
        r[0] for r in ddl("SELECT state FROM build_tasks WHERE build_id = :b AND stage <> 'acquire'", b=build)
    } == {"cancelled"}
    assert account_counters(ws)[1:] == (0, 0) and ledger_balance(ws) == {
        "reserve": 2_000_000,
        "release": 2_000_000,
    }


@dataclass
class CancelsMidCall(ScriptedProvider):
    """The user cancels while the first explain call is in flight."""

    trigger: Callable[[], Awaitable[Any]] | None = None
    fired: bool = False
    after_cancel: list[str] = field(default_factory=list)

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        if self.fired:
            self.after_cancel.append(request.purpose)
        if not self.fired and self.trigger is not None:
            self.fired = True
            await self.trigger()
        return await super().generate(request)


async def test_cancelling_during_a_call_settles_it_and_starts_no_new_call(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    """A10: no new call starts, the in-flight usage settles, the remainder is released, the build never
    becomes ready."""
    owner, ws, build, app = await generation_build(client, idp, github, preset="balanced-v1", cap=5_000_000)
    provider = CancelsMidCall()

    async def trigger() -> None:
        assert (await cancel(owner, build)).status_code == 200

    provider.trigger = trigger
    outcomes = await drive(app, build, runtime=runtime_for(app, provider))
    # Index completes; explain either stops at its next earmark (failed) or, when every unit was already
    # in flight, completes, and the store then cancels instead of starting the next stage.
    assert outcomes[0] == "completed" and len(outcomes) == 2 and outcomes[1] in ("failed", "completed"), (
        outcomes
    )
    assert {a[0] for a in attempts(build)} == {"explain"}  # no later stage ever ran
    done = (await owner.get(f"/api/v1/builds/{build}")).json()
    assert done["status"] == "cancelled"
    recorded = attempts(build)
    assert recorded and all(a[1] == "succeeded" for a in recorded)  # in-flight calls settled
    # Calls already earmarked when the user cancelled may finish; nothing is earmarked afterwards.
    assert len(recorded) == len(provider.calls) <= 4
    settled = sum(a[2] for a in recorded)
    assert account_counters(ws) == (50_000_000, 0, settled)
    assert ledger_balance(ws) == {"reserve": 5_000_000, "settle": settled, "release": 5_000_000 - settled}
    assert (await owner.get(f"/api/v1/builds/{build}/quality")).status_code == 409
    events = [e["event_type"] for e in (await owner.get(f"/api/v1/builds/{build}/events")).json()["items"]]
    assert "build.cancel_requested" in events and events[-2:] == ["build.cancelled", "usage.updated"]


async def test_retry_reuses_cached_generation_and_reruns_only_the_checks(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    """A13: the checker was down, the draft is unchecked; a retry build for the same snapshot and
    configuration takes explanations and pages from the tenant cache and pays only for checks."""
    owner, _ws, build, app = await generation_build(client, idp, github, preset="balanced-v1", cap=5_000_000)
    outage = [ProviderError("transient", "provider_unavailable") for _ in range(200)]
    first = ScriptedProvider(faults={"check": outage})
    assert await drive(app, build, runtime=runtime_for(app, first)) == ["completed"] * 8
    assert not (await owner.get(f"/api/v1/builds/{build}/quality")).json()["publishable"]

    retried = await retry(owner, build)
    assert retried.status_code == 202, retried.text
    second_build = retried.json()["id"]
    assert (
        retried.json()["retry_of_id"] == build
        and retried.json()["snapshot_id"]
        == (await owner.get(f"/api/v1/builds/{build}")).json()["snapshot_id"]
    )
    second = ScriptedProvider()
    assert await drive(app, second_build, runtime=runtime_for(app, second)) == ["completed"] * 8
    assert set(second.calls) <= {"check", "claim_repair"} and "check" in second.calls
    report = (await owner.get(f"/api/v1/builds/{second_build}/quality")).json()
    assert report["counts"]["unchecked"] == 0 and report["publishable"], report


async def test_retry_rules(client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub) -> None:
    owner, _ws, build, app = await generation_build(client, idp, github, preset="replay-v2")
    running = await retry(owner, build)
    assert running.status_code == 409 and running.json()["error"]["code"] == "BUILD_ACTIVE"
    await drive(app, build, runtime=runtime_for(app))
    stranger, _, stranger_project = await setup_project(client, idp, github)
    snapshot = await scanned_snapshot(stranger, app, stranger_project)
    foreign = (await make_estimate(stranger, stranger_project, snapshot["id"], preset="replay-v2")).json()
    mismatch = await retry(owner, build, estimate_id=foreign["id"])
    assert mismatch.status_code == 422 and mismatch.json()["error"]["code"] == "ESTIMATE_MISMATCH"
    assert (await retry(stranger, build)).status_code == 404
    assert (await cancel(stranger, build)).status_code == 404
    assert (await stranger.get(f"/api/v1/builds/{build}/events")).status_code == 404
    again = await retry(owner, build)
    assert again.status_code == 202 and again.json()["retry_of_id"] == build


async def test_events_are_ordered_and_resumable(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, build, app = await generation_build(client, idp, github, preset="replay-v2")
    await drive(app, build, runtime=runtime_for(app))
    page = (await owner.get(f"/api/v1/builds/{build}/events", params={"limit": 5})).json()
    assert [e["sequence"] for e in page["items"]] == [1, 2, 3, 4, 5]
    rest = (await owner.get(f"/api/v1/builds/{build}/events", params={"after": page["next_after"]})).json()
    assert rest["items"][0]["sequence"] == 6 and rest["items"][-1]["event_type"] == "build.ready"


async def test_cancel_stops_the_next_earmark(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With one call in flight at a time, the call running at cancel time settles and the next unit is
    refused at its earmark: exactly one provider call happens (A10)."""
    from app.jobs import generation

    monkeypatch.setattr(generation, "MAX_CONCURRENT_CALLS", 1)
    owner, ws, build, app = await generation_build(client, idp, github, preset="balanced-v1", cap=5_000_000)
    provider = CancelsMidCall()

    async def trigger() -> None:
        assert (await cancel(owner, build)).status_code == 200

    provider.trigger = trigger
    assert await drive(app, build, runtime=runtime_for(app, provider)) == ["completed", "failed"]
    assert provider.calls == ["explain"] and provider.after_cancel == []
    assert (await owner.get(f"/api/v1/builds/{build}")).json()["status"] == "cancelled"
    (only,) = attempts(build)
    assert only[1] == "succeeded" and account_counters(ws) == (50_000_000, 0, only[2])
