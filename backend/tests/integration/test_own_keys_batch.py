"""Own provider keys and batch processing (M6, T24; 07 "BYOK", "optional submit_batch/get_batch").

Exit gate: persisted batch IDs and keys, no silent price fallback. Own-key builds spend within their cap
on the workspace's key and never touch the plan allowance; batches survive waits and restarts.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.adapters.models.replay import ReplayGateway
from app.core.db import session_factory
from app.engine import catalog
from app.engine.gateway import BatchItem, GenerationRequest, GenerationResult, ProviderError
from app.jobs import control
from app.jobs.stages import Runtime
from tests.integration.auth_support import FakeIdP, ddl, unsafe
from tests.integration.conftest import RUNTIME_URL, SYSTEM_URL, needs_db, needs_system
from tests.integration.test_budgets import account_counters, make_estimate, start_from
from tests.integration.test_builds import drive
from tests.integration.test_generation import ScriptedProvider
from tests.integration.test_github import FakeGitHub, github  # noqa: F401
from tests.integration.test_publication import ready_build
from tests.integration.test_workspaces import NewUser, code, join, new_user  # noqa: F401
from tests.unit.test_catalog_estimate import PRICED

pytestmark = [pytest.mark.integration, needs_db]
GOOD_KEY = "sk-ant-test-" + "a" * 32


@dataclass
class KeyedProvider(ScriptedProvider):
    """The provider as seen through one API key; records which key served each call."""

    key: str = ""
    keys_used: list[str] = field(default_factory=list)

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.keys_used.append(self.key)
        return await super().generate(request)


@dataclass
class BatchingProvider(ScriptedProvider):
    """Accepts batches, ends them when told to, and errors the requests named in `fail`."""

    ended: bool = False
    batches: dict[str, list[tuple[str, GenerationRequest]]] = field(default_factory=dict)
    fail: set[int] = field(default_factory=set)  # positions within the batch to error
    polls: int = 0

    async def submit_batch(self, items: list[tuple[str, GenerationRequest]]) -> str:
        batch_id = f"msgbatch_{secrets.token_hex(6)}"
        self.batches[batch_id] = items
        return batch_id

    async def batch_ended(self, batch_id: str) -> bool:
        self.polls += 1
        return self.ended

    async def batch_results(self, batch_id: str, requests: dict[str, GenerationRequest]) -> list[BatchItem]:
        out = []
        for position, (custom_id, request) in enumerate(self.batches[batch_id]):
            if position in self.fail:
                out.append(BatchItem(custom_id, None, ProviderError("permanent", "batch_errored")))
                continue
            answer = await self.replay.generate(request)
            out.append(
                BatchItem(custom_id, GenerationResult(answer.output, answer.usage, None, request.model.id))
            )
        return out


def with_batch_price(app: Any) -> None:
    model = {
        **PRICED["models"][0],
        "batch_price": {"input_per_mtok_micro_usd": 1_500_000, "output_per_mtok_micro_usd": 7_500_000},
    }  # type: ignore[dict-item]
    app.state.catalog = catalog.Catalog.model_validate(
        {
            "revision": PRICED["revision"],
            "models": [catalog.REPLAY_MODEL.model_dump(), model],
            "presets": [catalog.REPLAY_PRESET.model_dump(), *PRICED["presets"]],  # type: ignore[list-item]
        }
    )


def runtime(app: Any, operator: Any = None, own: Any = None) -> Runtime:
    assert RUNTIME_URL
    gateways: dict[str, Any] = {"replay": ReplayGateway()}
    if operator is not None:
        gateways["anthropic"] = operator
    return Runtime(
        sessions=session_factory(create_async_engine(RUNTIME_URL)),
        store=app.state.objects,
        github=app.state.github_app,
        allow_unsandboxed=True,
        catalog=app.state.catalog,
        gateways=gateways,
        retry_delays=(0.0,),
        sealer=app.state.sealer,
        own_key_gateway=own,
    )


async def save_key(owner: httpx.AsyncClient, ws: str, key: str = GOOD_KEY) -> httpx.Response:
    return await unsafe(
        owner, "PUT", f"/api/v1/workspaces/{ws}/provider-keys/anthropic", json={"api_key": key}
    )


def accept_good_keys(app: Any) -> None:
    async def verifier(provider: str, key: str) -> bool:
        if key.startswith("sk-ant-down"):
            raise RuntimeError("provider outage")
        return key.startswith("sk-ant-test-")

    app.state.key_verifier = verifier


async def resume(app: Any, build: str, rt: Runtime) -> list[str]:
    """Make a waiting task due and let the reconciler notify it, then drive the build again."""
    ddl(
        "UPDATE build_tasks SET next_attempt_at = now(), notified_at = NULL WHERE build_id = :b AND state = 'retry_wait'",
        b=build,
    )
    assert SYSTEM_URL
    async with session_factory(create_async_engine(SYSTEM_URL))() as sys_db:
        await control.reconcile_once(sys_db)
    return await drive(app, build, runtime=rt)


# ---------------------------------------------------------------- own keys
async def test_owners_store_a_checked_key_that_is_never_shown_again(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, new_user: NewUser
) -> None:
    owner, ws, _project, _build, app = await ready_build(client, idp, github)
    accept_good_keys(app)
    assert code(await save_key(owner, ws, "sk-ant-bad-" + "b" * 32)) == "KEY_REJECTED"
    outage = await save_key(owner, ws, "sk-ant-down-" + "c" * 32)
    assert outage.status_code == 502 and outage.json()["error"]["retryable"] is True
    assert code(await save_key(owner, ws, "not a key")) in ("KEY_INVALID", "VALIDATION_FAILED")
    saved = await save_key(owner, ws)
    assert saved.status_code == 200, saved.text
    assert saved.json()["hint"] == f"{GOOD_KEY[:7]}…{GOOD_KEY[-4:]}" and GOOD_KEY not in saved.text
    sealed = ddl("SELECT sealed_key FROM provider_keys WHERE workspace_id = :w AND revoked_at IS NULL", w=ws)[
        0
    ][0]
    assert GOOD_KEY.encode() not in bytes(sealed)
    admin = await new_user()
    await join(owner, ws, admin, "admin")
    listed = await admin.get(f"/api/v1/workspaces/{ws}/provider-keys")
    assert listed.status_code == 200 and GOOD_KEY not in listed.text and listed.json()["items"][0]["hint"]
    assert (await save_key(admin, ws)).status_code == 403  # owners only
    audit = ddl(
        "SELECT metadata::text FROM audit_events WHERE workspace_id = :w AND action LIKE 'provider_key.%'",
        w=ws,
    )
    assert audit and all(GOOD_KEY not in row[0] for row in audit)
    assert (
        await unsafe(owner, "DELETE", f"/api/v1/workspaces/{ws}/provider-keys/anthropic")
    ).status_code == 204
    assert (
        ddl("SELECT count(*) FROM provider_keys WHERE workspace_id = :w AND sealed_key IS NOT NULL", w=ws)[0][
            0
        ]
        == 0
    )


async def test_an_own_key_build_spends_on_that_key_within_its_cap_and_not_the_allowance(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, ws, project, build, app = await ready_build(client, idp, github)
    accept_good_keys(app)
    app.state.settings = app.state.settings.model_copy(update={"anthropic_api_key": None})  # no operator key
    snapshot = str(ddl("SELECT snapshot_id FROM builds WHERE id = :b", b=build)[0][0])
    presets = (await owner.get("/api/v1/catalog/presets", params={"workspace_id": ws})).json()["items"]
    assert [p["name"] for p in presets] == ["replay-v2"]  # no key yet, no operator key
    assert (await save_key(owner, ws)).status_code == 200
    presets = (await owner.get("/api/v1/catalog/presets", params={"workspace_id": ws})).json()["items"]
    assert {p["name"]: p["own_key"] for p in presets} == {"replay-v2": False, "balanced-v1": True}
    stranger_ws = str(ddl("SELECT id FROM workspaces WHERE id <> :w LIMIT 1", w=ws)[0][0])
    assert (
        await owner.get("/api/v1/catalog/presets", params={"workspace_id": stranger_ws})
    ).status_code == 404

    estimate = (await make_estimate(owner, project, snapshot, preset="balanced-v1", guide_pages=3)).json()
    allowance_route = await start_from(owner, project, estimate["id"], cap=2_000_000)
    assert allowance_route.status_code == 409 and code(allowance_route) == "PRESET_UNAVAILABLE"
    no_cap = await unsafe(
        owner,
        "POST",
        f"/api/v1/projects/{project}/builds",
        json={"estimate_id": estimate["id"], "funding": "own_key"},
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )
    assert no_cap.status_code == 422 and code(no_cap) == "CAP_REQUIRED"
    before = account_counters(ws)
    started = await unsafe(
        owner,
        "POST",
        f"/api/v1/projects/{project}/builds",
        json={"estimate_id": estimate["id"], "funding": "own_key", "cap_micro_usd": 3_000_000},
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )
    assert started.status_code == 202, (
        started.text,
        ddl("SELECT status, config->>'funding' FROM builds WHERE project_id = :p", p=project),
    )
    built = started.json()["id"]
    providers: list[KeyedProvider] = []

    def own(key: str) -> KeyedProvider:
        providers.append(KeyedProvider(key=key))
        return providers[-1]

    await drive(app, built, runtime=runtime(app, own=own))
    status = (await owner.get(f"/api/v1/builds/{built}")).json()
    assert status["status"] == "ready", status
    assert providers and all(k == GOOD_KEY for p in providers for k in p.keys_used) and providers[0].keys_used
    settled = status["usage"]["settled_micro_usd"]
    assert 0 < settled <= 3_000_000
    assert ddl("SELECT usage_account_id FROM builds WHERE id = :b", b=built)[0][0] is None
    assert account_counters(ws) == before  # the plan allowance was neither reserved nor spent
    assert ddl("SELECT count(*) FROM usage_entries WHERE build_id = :b", b=built)[0][0] == 0

    # A key removed while a build runs stops it at its next call.
    again = await unsafe(
        owner,
        "POST",
        f"/api/v1/projects/{project}/builds",
        json={
            "estimate_id": (
                await make_estimate(owner, project, snapshot, preset="balanced-v1", guide_pages=3)
            ).json()["id"],
            "funding": "own_key",
            "cap_micro_usd": 3_000_000,
        },
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )
    assert again.status_code == 202, again.text
    assert (
        await unsafe(owner, "DELETE", f"/api/v1/workspaces/{ws}/provider-keys/anthropic")
    ).status_code == 204
    await drive(app, again.json()["id"], runtime=runtime(app, own=own))
    stopped = (await owner.get(f"/api/v1/builds/{again.json()['id']}")).json()
    assert stopped["status"] == "failed" and any(
        s["error_code"] == "provider_key_missing" for s in stopped["stages"]
    )
    missing = await unsafe(
        owner,
        "POST",
        f"/api/v1/projects/{project}/builds",
        json={"estimate_id": estimate["id"], "funding": "own_key", "cap_micro_usd": 1_000_000},
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )
    assert missing.status_code in (409, 404) and code(missing) in (
        "NO_PROVIDER_KEY",
        "ESTIMATE_STALE",
        "ESTIMATE_EXPIRED",
    )


# ---------------------------------------------------------------- batches
async def test_batch_estimates_need_a_batch_price_and_lower_only_the_low_bound(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, project, build, app = await ready_build(client, idp, github)
    snapshot = str(ddl("SELECT snapshot_id FROM builds WHERE id = :b", b=build)[0][0])
    no_price = await make_estimate(owner, project, snapshot, preset="balanced-v1", batch=True)
    assert no_price.status_code == 422 and code(no_price) == "BATCH_UNAVAILABLE"  # never a guessed price
    with_batch_price(app)
    standard = (await make_estimate(owner, project, snapshot, preset="balanced-v1")).json()
    batched = (await make_estimate(owner, project, snapshot, preset="balanced-v1", batch=True)).json()
    assert batched["cost_micro_usd"]["low"] < standard["cost_micro_usd"]["low"]
    assert batched["cost_micro_usd"]["high"] == standard["cost_micro_usd"]["high"]  # fallback is standard
    assert batched["config"]["batch"] is True and "batch" not in standard["config"]
    assert (
        code(await make_estimate(owner, project, snapshot, preset="replay-v2", batch=True))
        == "BATCH_UNAVAILABLE"
    )


@needs_system
async def test_a_batch_survives_waits_and_falls_back_for_what_it_could_not_answer(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, project, build, app = await ready_build(client, idp, github)
    with_batch_price(app)
    snapshot = str(ddl("SELECT snapshot_id FROM builds WHERE id = :b", b=build)[0][0])
    estimate = (
        await make_estimate(owner, project, snapshot, preset="balanced-v1", batch=True, guide_pages=3)
    ).json()
    started = await start_from(owner, project, estimate["id"], cap=5_000_000)
    assert started.status_code == 202, started.text
    built = started.json()["id"]
    provider = BatchingProvider(fail={0})
    rt = runtime(app, operator=provider)
    outcomes = await drive(app, built, runtime=rt)
    assert outcomes[-1] == "waiting", outcomes
    [(batch_id, state, items)] = ddl(
        "SELECT provider_batch_id, state, jsonb_array_length(items) FROM provider_batches WHERE build_id = :b",
        b=built,
    )
    assert (
        state == "in_progress" and batch_id in provider.batches and items == len(provider.batches[batch_id])
    )
    batched = ddl(
        "SELECT count(*) FROM provider_attempts WHERE build_id = :b AND state = 'batched'", b=built
    )[0][0]
    assert batched == items
    attempts_before = ddl(
        "SELECT attempt_count FROM build_tasks WHERE build_id = :b AND stage = 'explain'", b=built
    )[0][0]

    assert (await resume(app, built, rt))[-1] == "waiting"  # not ended yet: wait again
    assert (
        ddl("SELECT attempt_count FROM build_tasks WHERE build_id = :b AND stage = 'explain'", b=built)[0][0]
        == attempts_before
    )
    assert (
        ddl("SELECT count(*) FROM provider_attempts WHERE build_id = :b AND state = 'batched'", b=built)[0][0]
        == batched
    )

    provider.ended = True
    sync_before = provider.calls.count("explain")
    await resume(app, built, rt)
    status = (await owner.get(f"/api/v1/builds/{built}")).json()
    assert status["status"] == "ready", status
    assert ddl("SELECT state FROM provider_batches WHERE build_id = :b", b=built)[0][0] == "completed"
    rows = ddl(
        "SELECT state, error_code, price_snapshot->>'batch' FROM provider_attempts WHERE build_id = :b"
        " AND price_snapshot->>'batch' = 'true' ORDER BY attempt_number",
        b=built,
    )
    assert sorted(r[0] for r in rows) == sorted(["failed"] + ["succeeded"] * (len(rows) - 1))
    assert provider.calls.count("explain") == sync_before + 1  # only the errored request ran synchronously
    assert 0 < status["usage"]["settled_micro_usd"] <= 5_000_000
