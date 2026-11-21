"""Grounded generation through the provider gateway (T11; 07, 11, 19; A08, A09).

The pipeline runs through the real API, stage handlers, runner, ledger and cache. The deterministic
replay gateway answers for the free preset; a scripted fake provider stands in for a priced provider and
injects transient errors, invalid answers, refusals, timeouts and lost leases. No real provider is called.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.adapters.models.replay import ReplayGateway
from app.core.db import session_factory
from app.engine import GENERATION_PROMPT_VERSION
from app.engine.gateway import GenerationRequest, GenerationResult, ProviderError, Usage
from app.jobs import runner
from app.jobs.envelope import TaskEnvelope
from app.jobs.stages import Runtime
from tests.integration.auth_support import FakeIdP, ddl
from tests.integration.conftest import RUNTIME_URL, needs_db
from tests.integration.test_budgets import (
    account_counters,
    configure,
    ledger_balance,
    make_estimate,
    scanned_snapshot,
    start_from,
)
from tests.integration.test_builds import drive, setup_project
from tests.integration.test_github import FakeGitHub, github  # noqa: F401

pytestmark = [pytest.mark.integration, needs_db]

GENERATION_STAGES = ["acquire", "index", "explain", "overview", "plan", "guide", "verify", "check", "render"]


@dataclass
class ScriptedProvider:
    """Replay answers with realistic token usage; `faults[purpose]` lists errors to raise first."""

    faults: dict[str, list[Any]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    replay: ReplayGateway = field(default_factory=ReplayGateway)

    prompts: list[str] = field(default_factory=list)

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.calls.append(request.purpose)
        self.prompts.append(request.user)
        queue = self.faults.get(request.purpose, [])
        if queue:
            fault = queue.pop(0)
            if isinstance(fault, ProviderError):
                raise fault
            if fault == "invalid":
                return GenerationResult({"not": "valid"}, Usage(1000, 200), "req_invalid", request.model.id)
        answer = await self.replay.generate(request)
        usage = Usage(
            input_tokens=request.input_token_bound() // 2, output_tokens=min(400, request.max_output_tokens)
        )
        return GenerationResult(answer.output, usage, f"req_{len(self.calls)}", request.model.id)


def runtime_for(app: Any, provider: ScriptedProvider | None = None) -> Runtime:
    assert RUNTIME_URL
    gateways: dict[str, Any] = {"replay": ReplayGateway()}
    if provider is not None:
        gateways["anthropic"] = provider  # the priced test catalog binds its model to this provider
    return Runtime(
        sessions=session_factory(create_async_engine(RUNTIME_URL)),
        store=app.state.objects,
        github=app.state.github_app,
        public_github=app.state.public_github,
        allow_unsandboxed=True,
        catalog=app.state.catalog,
        gateways=gateways,
        retry_delays=(0.0,),
    )


async def generation_build(
    client: httpx.AsyncClient,
    idp: FakeIdP,
    gh: FakeGitHub,
    *,
    preset: str,
    cap: int | None = None,
    **config: Any,
) -> tuple[httpx.AsyncClient, str, str, Any]:
    owner, ws, project = await setup_project(client, idp, gh)
    app = configure(client)
    snapshot = await scanned_snapshot(owner, app, project)
    estimate = (
        await make_estimate(owner, project, snapshot["id"], preset=preset, guide_pages=3, **config)
    ).json()
    started = await start_from(owner, project, estimate["id"], cap=cap)
    assert started.status_code == 202, started.text
    return owner, ws, started.json()["id"], app


def attempts(build: str) -> list[Any]:
    return ddl(
        "SELECT purpose, state, settled_micro_usd, max_reserved_micro_usd, error_code, provider_request_id"
        " FROM provider_attempts WHERE build_id = :b ORDER BY created_at, attempt_number",
        b=build,
    )


async def drive_with_retries(app: Any, build: str, rt: Runtime) -> list[str]:
    """drive(), and run tasks waiting to retry at once instead of after their backoff, as the reconciler
    would once `next_attempt_at` passes."""
    outcomes = await drive(app, build, runtime=rt)
    while outcomes and outcomes[-1] == "retry_wait":
        [(task, payload)] = ddl(
            "SELECT t.id, o.payload FROM build_tasks t JOIN outbox o ON o.aggregate_id = t.id"
            " WHERE t.build_id = :b AND t.state = 'retry_wait' ORDER BY o.created_at DESC LIMIT 1",
            b=build,
        )
        ddl("UPDATE build_tasks SET next_attempt_at = now() WHERE id = :t", t=task)
        outcomes.append(await runner.process(rt, TaskEnvelope.model_validate(payload)))
        outcomes += await drive(app, build, runtime=rt)
    return outcomes


# ---------------------------------------------------------------- free replay preset, full pipeline
async def test_replay_preset_runs_the_whole_generation_pipeline(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, ws, build, app = await generation_build(client, idp, github, preset="replay-v2")
    outcomes = await drive(app, build, runtime=runtime_for(app))
    assert outcomes == ["completed"] * 8, outcomes
    status = (await owner.get(f"/api/v1/builds/{build}")).json()
    assert status["status"] == "ready"
    assert [s["stage"] for s in status["stages"]] == GENERATION_STAGES
    assert status["prompt_version"] == GENERATION_PROMPT_VERSION == "0.5.0"

    cards = {r[0]: r[1] for r in ddl("SELECT symbol_key, card FROM symbols WHERE build_id = :b", b=build)}
    assert cards["main"]["summary"] == "Entry point."
    assert (
        cards["main"]["status"] == "generated" and cards["main"]["provenance"]["model"] == "replay/replay-v2"
    )
    assert cards["main"]["source"]["params"] == ["argv"]  # parser evidence kept beside the explanation
    files = ddl(
        "SELECT f.path, e.status FROM file_explanations e JOIN source_files f ON f.id = e.source_file_id WHERE e.build_id = :b",
        b=build,
    )
    assert sorted(files) == [
        ("src/app.py", "generated"),
        ("src/util.py", "generated"),
        ("web/index.ts", "generated"),
    ]

    pages = ddl(
        "SELECT slug, status, markdown_key FROM guide_pages WHERE build_id = :b ORDER BY ordinal", b=build
    )
    assert len(pages) >= 1 and all(p[1] == "written" for p in pages), pages
    objects = app.state.objects.objects
    markdown = objects[pages[0][2]][0].decode()
    assert markdown.startswith("# ") and "[[" in markdown
    manifest_key = ddl("SELECT manifest_key FROM builds WHERE id = :b", b=build)[0][0]
    manifest = json.loads(objects[manifest_key][0])
    assert manifest["generator"] == "replay-v2" and manifest["catalog"] == "catalog-2026-11-b"
    assert [g["slug"] for g in manifest["guide"]] == [p[0] for p in pages]
    assert set(manifest["artifacts"]) == {"overview", "guide_plan"}

    recorded = attempts(build)
    assert {a[0] for a in recorded} == {"explain", "overview", "plan", "guide", "check"}
    assert all(a[1] == "succeeded" and a[2] == 0 for a in recorded)
    assert ledger_balance(ws) == {}  # zero cap, zero cost

    index = (await owner.get(f"/api/v1/builds/{build}/guide")).json()
    assert index["overview"]["status"] == "generated" and index["overview"]["reading_order"]
    assert [p["slug"] for p in index["pages"]] == [p[0] for p in pages]
    page = (await owner.get(f"/api/v1/builds/{build}/guide/{pages[0][0]}")).json()
    assert page["markdown"] == markdown and page["citations"]
    assert all(c["path"] for c in page["citations"])  # citations resolve to immutable file IDs of this build
    app_py = ddl(
        "SELECT f.id FROM source_files f JOIN builds b ON b.snapshot_id = f.snapshot_id WHERE b.id = :b AND f.path = 'src/app.py'",
        b=build,
    )[0][0]
    viewed = (await owner.get(f"/api/v1/builds/{build}/files/{app_py}")).json()
    assert (
        viewed["explanation"]["status"] == "generated"
        and viewed["explanation"]["chunks"][0]["start_line"] == 1
    )
    assert (await owner.get(f"/api/v1/builds/{build}/guide/no-such-page")).status_code == 404
    stranger, _, _ = await setup_project(client, idp, github)
    for path in (f"/api/v1/builds/{build}/guide", f"/api/v1/builds/{build}/guide/{pages[0][0]}"):
        assert (await stranger.get(path)).status_code == 404


# ---------------------------------------------------------------- priced provider
async def test_priced_calls_are_earmarked_settled_and_released(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, ws, build, app = await generation_build(client, idp, github, preset="balanced-v1", cap=5_000_000)
    provider = ScriptedProvider()
    assert await drive(app, build, runtime=runtime_for(app, provider)) == ["completed"] * 8
    recorded = attempts(build)
    assert recorded and all(a[1] == "succeeded" for a in recorded)
    assert all(0 < a[2] <= a[3] for a in recorded)  # actual cost never above the earmark
    settled = sum(a[2] for a in recorded)
    done = (await owner.get(f"/api/v1/builds/{build}")).json()
    assert done["status"] == "ready"
    assert done["usage"] == {
        "cap_micro_usd": 5_000_000,
        "reserved_micro_usd": 0,
        "settled_micro_usd": settled,
    }
    assert account_counters(ws) == (50_000_000, 0, settled)
    assert ledger_balance(ws) == {"reserve": 5_000_000, "settle": settled, "release": 5_000_000 - settled}
    assert {a[5] for a in recorded} == {f"req_{i + 1}" for i in range(len(recorded))}  # provider IDs kept


async def test_budget_exhaustion_stops_before_a_call_and_keeps_the_ledger_balanced(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, ws, build, app = await generation_build(client, idp, github, preset="balanced-v1", cap=1_000)
    provider = ScriptedProvider()
    assert await drive(app, build, runtime=runtime_for(app, provider)) == [
        "completed",
        "failed",
    ]  # the explain task fails; the build is budget_exhausted
    assert provider.calls == []  # generation stops before a request would exceed the cap (01)
    done = (await owner.get(f"/api/v1/builds/{build}")).json()
    assert done["status"] == "budget_exhausted"
    assert attempts(build) == []
    assert account_counters(ws) == (50_000_000, 0, 0)
    assert ledger_balance(ws) == {"reserve": 1_000, "release": 1_000}


async def test_transient_errors_retry_and_invalid_answers_get_one_repair(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, build, app = await generation_build(client, idp, github, preset="balanced-v1", cap=5_000_000)
    provider = ScriptedProvider(
        faults={
            "overview": [ProviderError("transient", "provider_rate_limited", retry_after_seconds=0.0)],
            "plan": ["invalid"],
        }
    )
    assert await drive(app, build, runtime=runtime_for(app, provider)) == ["completed"] * 8
    by_purpose: dict[str, list[Any]] = {}
    for a in attempts(build):
        by_purpose.setdefault(a[0], []).append(a)
    assert [(a[1], a[4]) for a in by_purpose["overview"]] == [
        ("failed", "provider_rate_limited"),
        ("succeeded", None),
    ]
    assert by_purpose["overview"][0][2] == 0  # a rate-limited request is not billed
    assert [(a[1], a[4]) for a in by_purpose["plan"]] == [("failed", "output_invalid")]
    assert [a[1] for a in by_purpose["repair"]] == ["succeeded"]
    assert by_purpose["plan"][0][2] > 0  # the invalid answer was still billed
    assert (await owner.get(f"/api/v1/builds/{build}")).json()["status"] == "ready"


async def test_refusal_falls_back_to_a_labeled_source_only_card(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, build, app = await generation_build(client, idp, github, preset="balanced-v1", cap=5_000_000)
    provider = ScriptedProvider(
        faults={"explain": [ProviderError("permanent", "provider_refusal", usage=Usage(10, 0))]}
    )
    assert await drive(app, build, runtime=runtime_for(app, provider)) == ["completed"] * 8
    statuses = sorted(r[0] for r in ddl("SELECT status FROM file_explanations WHERE build_id = :b", b=build))
    assert statuses == ["failed", "generated", "generated"]
    failed = ddl(
        "SELECT y.card FROM symbols y JOIN file_explanations e ON e.source_file_id = y.source_file_id"
        " AND e.build_id = y.build_id WHERE y.build_id = :b AND e.status = 'failed' LIMIT 1",
        b=build,
    )
    if failed:  # a file whose chunk was refused keeps a card, labeled as a fallback, never as model output
        assert (
            failed[0][0]["status"] == "failed"
            and failed[0][0]["provenance"]["generator"] == "replay-fallback"
        )
    assert (await owner.get(f"/api/v1/builds/{build}")).json()["status"] == "ready"


async def test_unknown_provider_outcome_keeps_its_hold_after_the_build_ends(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, ws, build, app = await generation_build(client, idp, github, preset="balanced-v1", cap=5_000_000)
    provider = ScriptedProvider(faults={"overview": [ProviderError("unknown", "provider_timeout")]})
    outcomes = await drive_with_retries(app, build, runtime_for(app, provider))
    # Not retried within the call, which would add a hold per try: the stage stops and its task retries.
    assert outcomes.count("retry_wait") == 1 and outcomes.count("completed") == 8
    unknown = [a for a in attempts(build) if a[1] == "unknown"]
    assert len(unknown) == 1 and unknown[0][2] is None
    hold = unknown[0][3]
    settled = sum(a[2] or 0 for a in attempts(build))
    done = (await owner.get(f"/api/v1/builds/{build}")).json()
    assert done["status"] == "ready"
    assert done["usage"]["reserved_micro_usd"] == hold  # conservative hold until reconciliation (07)
    assert account_counters(ws) == (50_000_000, hold, settled)
    assert ledger_balance(ws)["release"] == 5_000_000 - settled - hold


async def test_a_provider_that_never_answers_fails_the_build_instead_of_falling_back(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    """Dropped connections (unknown outcomes) on every call: each task attempt makes one call per unit,
    the stage stops instead of finishing on source-only text, and the build fails after the task's
    retries, holding one maximum per attempt made."""
    from app.jobs import store

    owner, _ws, build, app = await generation_build(client, idp, github, preset="balanced-v1", cap=5_000_000)
    outage = [ProviderError("unknown", "provider_connection") for _ in range(200)]
    provider = ScriptedProvider(faults={"explain": outage})
    outcomes = await drive_with_retries(app, build, runtime_for(app, provider))
    assert outcomes[-1] == "failed" and outcomes.count("retry_wait") == store.MAX_ATTEMPTS - 1
    done = (await owner.get(f"/api/v1/builds/{build}")).json()
    assert done["status"] == "failed"
    assert not ddl("SELECT 1 FROM file_explanations WHERE build_id = :b", b=build)  # no fallback text
    recorded = attempts(build)
    assert {a[0] for a in recorded} == {"explain"} and all(a[1] == "unknown" for a in recorded)
    chunks = len(recorded) // store.MAX_ATTEMPTS
    assert len(recorded) == chunks * store.MAX_ATTEMPTS  # one call per chunk per task attempt
    assert done["usage"]["reserved_micro_usd"] == sum(a[3] for a in recorded)


@dataclass
class SlowProvider(ScriptedProvider):
    """Holds each call open for a while, so a stage's calls overlap like real provider calls."""

    delay: float = 0.3
    started: asyncio.Event = field(default_factory=asyncio.Event)
    begun: list[str] = field(default_factory=list)

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.begun.append(request.purpose)
        self.started.set()
        await asyncio.sleep(self.delay)
        return await super().generate(request)


async def test_calls_wait_for_in_flight_holds_instead_of_ending_the_build(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each call holds its worst case, which is far above its actual cost. A cap that fits any one call
    once the earlier ones settled, but not several worst cases at once, used to end the build as
    budget_exhausted while the money was there (seen live with concurrent guide calls). Now a call waits
    for the build's in-flight calls to settle. The explain stage's three calls show it."""
    from app.jobs import calls

    monkeypatch.setattr(calls, "HOLD_POLL_SECONDS", 0.05)
    _, _, probe, app = await generation_build(client, idp, github, preset="balanced-v1", cap=5_000_000)
    assert await drive(app, probe, rounds=2, runtime=runtime_for(app, SlowProvider(delay=0.0))) == [
        "completed",
        "completed",
    ]  # index, explain
    explain = [a for a in attempts(probe) if a[0] == "explain"]
    maxima = sorted((a[3] for a in explain), reverse=True)
    cap = maxima[0] + sum(a[2] for a in explain) + 1_000  # room for every call one at a time
    assert len(maxima) == 3 and maxima[0] + maxima[1] > cap  # but not for two worst cases at once

    owner, _ws, build, app = await generation_build(client, idp, github, preset="balanced-v1", cap=cap)
    provider = SlowProvider()
    assert await drive(app, build, rounds=2, runtime=runtime_for(app, provider)) == ["completed", "completed"]
    explained = [a for a in attempts(build) if a[0] == "explain"]
    assert len(explained) == 3 and all(a[1] == "succeeded" for a in explained)
    assert provider.calls.count("explain") == 3  # each answered by the provider, none stopped
    usage = (await owner.get(f"/api/v1/builds/{build}")).json()["usage"]
    assert usage["settled_micro_usd"] == sum(a[2] for a in explained) <= cap


async def test_a_call_cut_off_by_a_shutdown_is_unknown_and_keeps_its_hold(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, build, app = await generation_build(client, idp, github, preset="balanced-v1", cap=5_000_000)
    provider = SlowProvider(delay=30.0)
    rt = runtime_for(app, provider)
    assert await drive(app, build, rounds=1, runtime=rt) == ["completed"]  # index
    running = asyncio.create_task(drive(app, build, rounds=1, runtime=rt))  # explain, cut off mid-call
    await asyncio.wait_for(provider.started.wait(), timeout=10)
    await asyncio.sleep(0.1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    for _ in range(50):  # the cut-off calls settle in tasks of their own
        cut = [a for a in attempts(build) if a[0] == "explain"]
        if cut and all(a[1] != "started" for a in cut):
            break
        await asyncio.sleep(0.05)
    assert cut and all(a[1] == "unknown" and a[4] == "call_cancelled" and a[2] is None for a in cut)
    await asyncio.sleep(0.3)
    assert len(provider.begun) == len(cut)  # the stage stopped: no further call after the shutdown
    held = (await owner.get(f"/api/v1/builds/{build}")).json()["usage"]["reserved_micro_usd"]
    assert held == 5_000_000  # nothing settled or released: the cut-off calls may have been billed


async def test_unknown_holds_settle_at_their_maximum_after_a_day(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    from app.jobs import retention

    owner, ws, build, app = await generation_build(client, idp, github, preset="balanced-v1", cap=5_000_000)
    provider = ScriptedProvider(faults={"overview": [ProviderError("unknown", "provider_timeout")]})
    assert (await drive_with_retries(app, build, runtime_for(app, provider))).count("completed") == 8
    [hold] = [a[3] for a in attempts(build) if a[1] == "unknown"]
    settled = sum(a[2] or 0 for a in attempts(build))
    rt = runtime_for(app)

    assert (await retention.sweep_workspace(rt.sessions, rt.store, ws))["stale_holds"] == 0  # too recent
    assert (await owner.get(f"/api/v1/builds/{build}")).json()["usage"]["reserved_micro_usd"] == hold
    ddl(
        "UPDATE provider_attempts SET created_at = now() - interval '25 hours'"
        " WHERE build_id = :b AND state = 'unknown'",
        b=build,
    )
    assert (await retention.sweep_workspace(rt.sessions, rt.store, ws))["stale_holds"] == 1
    assert (await retention.sweep_workspace(rt.sessions, rt.store, ws))["stale_holds"] == 0  # idempotent
    [reconciled] = [a for a in attempts(build) if a[0] == "overview" and a[4] == "provider_timeout"]
    assert (reconciled[1], reconciled[2]) == ("reconciled", hold)  # settled at its maximum
    done = (await owner.get(f"/api/v1/builds/{build}")).json()
    assert done["usage"] == {
        "cap_micro_usd": 5_000_000,
        "reserved_micro_usd": 0,
        "settled_micro_usd": settled + hold,
    }
    assert account_counters(ws) == (50_000_000, 0, settled + hold)
    assert ledger_balance(ws) == {
        "reserve": 5_000_000,
        "settle": settled + hold,
        "release": 5_000_000 - settled - hold,
    }


async def test_crash_after_paid_output_reuses_the_checkpoint_without_paying_again(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    """A09: the worker settles and checkpoints the answer, then dies before completing the task. The
    retried task reuses the tenant cache entry: no second call, no second charge."""
    from app.jobs import stages, store

    owner, ws, build, app = await generation_build(client, idp, github, preset="balanced-v1", cap=5_000_000)
    provider = ScriptedProvider()
    rt = runtime_for(app, provider)
    assert await drive(app, build, rounds=1, runtime=rt) == ["completed"]  # index
    (payload,) = ddl(
        "UPDATE outbox SET published_at = now() WHERE published_at IS NULL AND payload->>'build_id' = :b RETURNING payload",
        b=build,
    )[0]
    envelope = TaskEnvelope.model_validate(payload)
    async with rt.sessions() as db:
        claimed = await store.claim(db, envelope)
    assert claimed is not None and claimed.stage == "explain"
    await stages.HANDLERS["explain"](rt, claimed)  # paid calls settle and checkpoint; then the worker dies
    paid = len(provider.calls)
    charged = account_counters(ws)[2]
    assert paid > 0 and charged > 0
    ddl("UPDATE build_tasks SET lease_until = now() - interval '1 second' WHERE id = :t", t=claimed.task_id)
    assert await runner.process(rt, envelope) == "completed"  # the expired lease is re-claimed
    assert len(provider.calls) == paid  # every chunk came from the checkpoint
    assert account_counters(ws)[2] == charged
    assert await drive(app, build, runtime=rt) == ["completed"] * 6
    assert (await owner.get(f"/api/v1/builds/{build}")).json()["status"] == "ready"


async def test_a_worker_that_lost_its_lease_cannot_settle_or_checkpoint(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    from app.jobs import stages, store

    _owner, ws, build, app = await generation_build(client, idp, github, preset="balanced-v1", cap=5_000_000)

    @dataclass
    class StealsLease(ScriptedProvider):
        """Another worker re-leases the task while this one waits for the provider."""

        stolen: bool = False

        async def generate(self, request: GenerationRequest) -> GenerationResult:
            if not self.stolen:
                self.stolen = True
                await asyncio.to_thread(
                    ddl,
                    "UPDATE build_tasks SET lease_generation = lease_generation + 1"
                    " WHERE build_id = :b AND state = 'leased'",
                    b=build,
                )
            return await super().generate(request)

    rt = runtime_for(app, StealsLease())
    assert await drive(app, build, rounds=1, runtime=rt) == ["completed"]
    (payload,) = ddl(
        "UPDATE outbox SET published_at = now() WHERE published_at IS NULL AND payload->>'build_id' = :b RETURNING payload",
        b=build,
    )[0]
    async with rt.sessions() as db:
        claimed = await store.claim(db, TaskEnvelope.model_validate(payload))
    assert claimed is not None
    with pytest.raises(store.LeaseLost):
        await stages.HANDLERS["explain"](rt, claimed)
    recorded = attempts(build)
    # Calls in flight when the lease was lost never settle; their outcome becomes unknown on re-claim.
    assert any(a[1] == "started" for a in recorded)
    assert {a[1] for a in recorded} <= {"started", "succeeded"}
    settled_before = [a for a in recorded if a[1] == "succeeded"]  # settled while the lease was still held
    assert ddl("SELECT count(*) FROM generation_cache WHERE workspace_id = :w", w=ws)[0][0] == len(
        settled_before
    )
    assert ledger_balance(ws).get("settle", 0) == sum(a[2] for a in settled_before)
    assert ledger_balance(ws)["reserve"] == 5_000_000


async def test_reclaimed_task_marks_unsettled_attempts_unknown_and_keeps_their_holds(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    """A09: a worker dies mid-call. On re-claim its attempts become unknown (never silently dropped or
    re-billed); their holds survive the build's release until reconciliation."""
    from app.jobs import stages, store

    owner, ws, build, app = await generation_build(client, idp, github, preset="balanced-v1", cap=5_000_000)

    class Killed(BaseException):
        """The worker process dies while waiting for the provider: no handler of its own runs, unlike a
        cancellation, whose cut-off calls settle as unknown (test_a_call_cut_off_by_a_shutdown...)."""

    @dataclass
    class Dies(ScriptedProvider):
        async def generate(self, request: GenerationRequest) -> GenerationResult:
            raise Killed

    rt = runtime_for(app, ScriptedProvider())
    assert await drive(app, build, rounds=1, runtime=rt) == ["completed"]
    (payload,) = ddl(
        "UPDATE outbox SET published_at = now() WHERE published_at IS NULL AND payload->>'build_id' = :b RETURNING payload",
        b=build,
    )[0]
    envelope = TaskEnvelope.model_validate(payload)
    dying = runtime_for(app, Dies())
    async with dying.sessions() as db:
        claimed = await store.claim(db, envelope)
    assert claimed is not None
    with pytest.raises(Killed):
        await stages.HANDLERS["explain"](dying, claimed)
    started = [a for a in attempts(build) if a[1] == "started"]
    assert started
    ddl("UPDATE build_tasks SET lease_until = now() - interval '1 second' WHERE id = :t", t=claimed.task_id)
    assert await runner.process(rt, envelope) == "completed"
    assert await drive(app, build, runtime=rt) == ["completed"] * 6
    recorded = attempts(build)
    unknown = [a for a in recorded if a[1] == "unknown"]
    assert len(unknown) == len(started) and all(a[4] == "lease_lost" for a in unknown)
    holds = sum(a[3] for a in unknown)
    settled = sum(a[2] or 0 for a in recorded)
    done = (await owner.get(f"/api/v1/builds/{build}")).json()
    assert done["status"] == "ready" and done["usage"]["reserved_micro_usd"] == holds
    assert account_counters(ws) == (50_000_000, holds, settled)


async def test_files_outside_the_scope_never_reach_a_provider_prompt(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    """src/app.py calls `helper` from src/util.py; with util.py outside the approved scope, no prompt may
    carry its path, signature or definitions (07), even as related context."""
    _owner, _ws, build, app = await generation_build(
        client, idp, github, preset="balanced-v1", cap=5_000_000, include=["src/app.py"]
    )
    provider = ScriptedProvider()
    assert await drive(app, build, runtime=runtime_for(app, provider)) == ["completed"] * 8
    assert provider.prompts
    for prompt in provider.prompts:
        assert "src/util.py" not in prompt and "def helper" not in prompt and "web/index.ts" not in prompt
        assert "hunter2" not in prompt and "AKIAABCDEFGHIJKLMNOP" not in prompt
