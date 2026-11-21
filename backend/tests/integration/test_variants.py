"""Language and level variants (M6, T22; 01 FR-026): a variant keeps its base's page and source anchors,
its citations stay valid against the same snapshot, and only the writer is told the audience."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from app.engine import GENERATION_PROMPT_VERSION
from app.engine.gateway import GenerationRequest, GenerationResult
from tests.integration.auth_support import FakeIdP, ddl, unsafe
from tests.integration.conftest import needs_db
from tests.integration.test_budgets import make_estimate, start_from
from tests.integration.test_builds import drive
from tests.integration.test_generation import ScriptedProvider, runtime_for
from tests.integration.test_github import FakeGitHub, github  # noqa: F401
from tests.integration.test_own_keys_batch import resume
from tests.integration.test_publication import ready_build
from tests.integration.test_workspaces import code

pytestmark = [pytest.mark.integration, needs_db]


@dataclass
class RecordingProvider(ScriptedProvider):
    systems: dict[str, list[str]] = field(default_factory=dict)

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.systems.setdefault(request.purpose, []).append(request.system)
        return await super().generate(request)


def snapshot_of(build: str) -> str:
    return str(ddl("SELECT snapshot_id FROM builds WHERE id = :b", b=build)[0][0])


def pages(build: str) -> list[tuple[str, int, Any]]:
    return [
        (r[0], r[1], r[2])
        for r in ddl(
            "SELECT slug, ordinal, source_ranges FROM guide_pages WHERE build_id = :b ORDER BY ordinal",
            b=build,
        )
    ]


async def variant(
    owner: httpx.AsyncClient,
    project: str,
    base: str,
    *,
    language: str,
    level: str,
    preset: str = "replay-v2",
    **extra: Any,
) -> httpx.Response:
    return await make_estimate(
        owner,
        project,
        snapshot_of(base),
        preset=preset,
        languages=[language],
        levels=[level],
        variant_of=base,
        **extra,
    )


async def test_a_variant_keeps_page_and_source_anchors_and_valid_citations(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, project, base, app = await ready_build(client, idp, github)
    # The caller's scope is ignored: a variant explains what its base explained.
    estimate = await variant(
        owner, project, base, language="de", level="beginner", max_files=1, guide_pages=1
    )
    assert estimate.status_code == 201, estimate.text
    assert estimate.json()["config"]["variant_of"] == base
    base_scope = ddl(
        "SELECT e.config FROM builds b JOIN estimates e ON e.id = b.estimate_id WHERE b.id = :b", b=base
    )[0][0]
    for key in ("include", "exclude", "max_files", "guide_pages"):
        assert estimate.json()["config"][key] == base_scope[key], key
    started = await start_from(owner, project, estimate.json()["id"])
    assert started.status_code == 202, started.text
    built = started.json()["id"]
    await drive(app, built, runtime=runtime_for(app))
    status = (await owner.get(f"/api/v1/builds/{built}")).json()
    assert (status["status"], status["language"], status["level"], status["variant_of"]) == (
        "ready",
        "de",
        "beginner",
        base,
    )
    assert status["prompt_version"] == GENERATION_PROMPT_VERSION == "0.5.0"

    assert pages(built) == pages(base) and pages(base)  # same slugs, order and source ranges
    base_files = {
        str(r[0]) for r in ddl("SELECT id FROM source_files WHERE snapshot_id = :s", s=snapshot_of(base))
    }
    citations = ddl(
        "SELECT source_file_id, deterministic FROM claim_checks WHERE build_id = :b AND current", b=built
    )
    assert citations and all(str(f) in base_files and d == "valid" for f, d in citations)
    assert (
        ddl("SELECT count(*) FROM artifacts WHERE build_id = :b AND kind = 'guide_plan'", b=built)[0][0] == 1
    )

    for build in (base, built):
        listed = (await owner.get(f"/api/v1/builds/{build}/variants")).json()["items"]
        assert [(v["id"], v["language"], v["level"], v["base"]) for v in listed] == [
            (base, "en", "standard", True),
            (built, "de", "beginner", False),
        ]
    slug = pages(base)[0][0]
    assert (await owner.get(f"/api/v1/builds/{built}/guide/{slug}")).status_code == 200  # the same anchor


async def test_only_the_writer_is_told_the_audience(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, project, base, app = await ready_build(client, idp, github)
    estimate = await variant(owner, project, base, language="id", level="expert", preset="balanced-v1")
    assert estimate.status_code == 201, estimate.text
    started = await start_from(owner, project, estimate.json()["id"], cap=5_000_000)
    assert started.status_code == 202, started.text
    provider = RecordingProvider()
    await drive(app, started.json()["id"], runtime=runtime_for(app, provider))
    assert (await owner.get(f"/api/v1/builds/{started.json()['id']}")).json()["status"] == "ready"
    for purpose in ("explain", "overview", "guide"):
        assert provider.systems[purpose] and all(
            "in Indonesian for expert readers" in s for s in provider.systems[purpose]
        )
    assert "plan" not in provider.systems  # the base's plan is reused
    assert all(
        "Indonesian" not in s for s in provider.systems.get("check", [])
    )  # the checker only judges support


async def test_variant_requests_are_validated(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, project, base, app = await ready_build(client, idp, github)
    unknown = await variant(owner, project, base, language="fr", level="standard")
    assert unknown.status_code == 422
    unchanged = await variant(owner, project, base, language="en", level="standard")
    assert unchanged.status_code == 422 and code(unchanged) == "VARIANT_UNCHANGED"
    missing = await make_estimate(
        owner, project, snapshot_of(base), preset="replay-v2", languages=["de"], variant_of=str(uuid.uuid4())
    )
    assert missing.status_code == 404
    first = await variant(owner, project, base, language="de", level="standard")
    started = await start_from(owner, project, first.json()["id"])
    await drive(app, started.json()["id"], runtime=runtime_for(app))
    nested = await variant(owner, project, started.json()["id"], language="id", level="standard")
    assert nested.status_code == 422 and code(nested) == "VARIANT_OF_VARIANT"
    # A plain build in German (no base) is a normal generation build with its own plan.
    german = await make_estimate(
        owner, project, snapshot_of(base), preset="replay-v2", languages=["de"], guide_pages=3
    )
    assert german.status_code == 201 and "variant_of" not in german.json()["config"]


async def family(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, languages: list[str], cap: int
) -> tuple[httpx.AsyncClient, Any, dict[str, Any], list[str]]:
    owner, _ws, project, base, app = await ready_build(client, idp, github)
    estimate = await make_estimate(
        owner, project, snapshot_of(base), preset="balanced-v1", languages=languages
    )
    assert estimate.status_code == 201, estimate.text
    started = await start_from(owner, project, estimate.json()["id"], cap=cap)
    assert started.status_code == 202, started.text
    first = started.json()["id"]
    builds = [first] + [
        str(r[0])
        for r in ddl(
            "SELECT id FROM builds WHERE config->'generation'->>'variant_of' = :b ORDER BY created_at",
            b=first,
        )
    ]
    return owner, app, estimate.json(), builds


async def test_one_estimate_builds_every_language_as_a_family(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    """FR-026: one estimate in three languages starts the base build and a variant per further language at
    once (one active build for the workspace limit of 2), each reserving its share of the cap; a variant
    copies the base's plan once the base has planned, and the family reads as variants of the base."""
    owner, app, estimate, builds = await family(client, idp, github, ["en", "de", "id"], cap=3_000_000)
    per_language = estimate["per_language_micro_usd"]
    assert estimate["cost_micro_usd"] == {"low": per_language["low"] * 3, "high": per_language["high"] * 3}
    base, *variants = builds
    assert len(variants) == 2
    rows = ddl(
        "SELECT b.id, b.config->'generation'->'languages', b.reserved_micro_usd, e.config->>'variant_of'"
        " FROM builds b JOIN estimates e ON e.id = b.estimate_id WHERE b.id = ANY(CAST(:ids AS uuid[]))",
        ids=builds,
    )
    by_id = {str(r[0]): r for r in rows}
    assert by_id[base][1] == ["en", "de", "id"] and by_id[base][3] is None
    assert [by_id[v][1] for v in variants] == [["de"], ["id"]] and all(by_id[v][3] == base for v in variants)
    assert sum(r[2] for r in rows) == 3_000_000 and {r[2] for r in rows} == {1_000_000}

    rt = runtime_for(app, ScriptedProvider())
    assert (await drive(app, variants[0], runtime=rt))[-1] == "waiting"  # the base has not planned yet
    await drive(app, base, runtime=rt)
    assert (await owner.get(f"/api/v1/builds/{base}")).json()["status"] == "ready"
    for v in variants:
        await resume(app, v, rt)
        await drive(app, v, runtime=rt)
        status = (await owner.get(f"/api/v1/builds/{v}")).json()
        assert status["status"] == "ready" and status["variant_of"] == base, status
        assert pages(v) == pages(base) and pages(base)
    listed = (await owner.get(f"/api/v1/builds/{base}/variants")).json()["items"]
    assert [(v["language"], v["base"]) for v in listed] == [("en", True), ("de", False), ("id", False)]


async def test_a_variant_fails_when_its_base_stops_before_planning(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, app, _estimate, builds = await family(client, idp, github, ["en", "de"], cap=2_000_000)
    base, variant_build = builds
    rt = runtime_for(app, ScriptedProvider())
    assert (await drive(app, variant_build, runtime=rt))[-1] == "waiting"
    cancelled = await unsafe(owner, "POST", f"/api/v1/builds/{base}/cancel", json={})
    assert cancelled.status_code in (200, 202), cancelled.text
    await drive(app, base, runtime=rt)
    assert (await owner.get(f"/api/v1/builds/{base}")).json()["status"] == "cancelled"
    await resume(app, variant_build, rt)
    status = (await owner.get(f"/api/v1/builds/{variant_build}")).json()
    assert status["status"] == "failed", status
    assert any(s["error_code"] == "base_unavailable" for s in status["stages"]), status["stages"]


async def test_several_languages_are_validated(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, project, base, _app = await ready_build(client, idp, github)
    repeated = await make_estimate(
        owner, project, snapshot_of(base), preset="balanced-v1", languages=["en", "en"]
    )
    assert repeated.status_code == 422
    variant_of_two = await make_estimate(
        owner, project, snapshot_of(base), preset="replay-v2", languages=["de", "id"], variant_of=base
    )
    assert variant_of_two.status_code == 422


async def test_variants_started_one_by_one_keep_the_one_active_build_rule(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    """Only the variants a several-language build starts with its base run beside it (the server marks them
    `family`); a variant started on its own counts as an active build of its project like any build."""
    owner, _ws, project, base, _app = await ready_build(client, idp, github)
    first = await variant(owner, project, base, language="de", level="standard")
    second = await variant(owner, project, base, language="id", level="standard")
    assert (await start_from(owner, project, first.json()["id"])).status_code == 202
    blocked = await start_from(owner, project, second.json()["id"])
    assert blocked.status_code == 409 and code(blocked) == "BUILD_ACTIVE", blocked.text
    assert all(
        r[0] is None for r in ddl("SELECT config->>'family' FROM builds WHERE project_id = :p", p=project)
    )
