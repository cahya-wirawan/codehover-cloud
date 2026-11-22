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
    assert status["prompt_version"] == GENERATION_PROMPT_VERSION == "0.6.0"

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
    unknown = await variant(owner, project, base, language="tlh", level="standard")
    assert unknown.status_code == 422
    japanese = await variant(owner, project, base, language="ja", level="standard")  # one of the 30
    assert japanese.status_code == 201, japanese.text
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
    client: httpx.AsyncClient,
    idp: FakeIdP,
    github: FakeGitHub,
    languages: list[str],
    cap: int,
    **config: Any,
) -> tuple[httpx.AsyncClient, Any, dict[str, Any], list[str]]:
    owner, _ws, project, base, app = await ready_build(client, idp, github)
    estimate = await make_estimate(
        owner, project, snapshot_of(base), preset="balanced-v1", languages=languages, **config
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


async def test_cancelling_the_base_cancels_its_language_variants(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    """The variants were started by the same request and need the base's plan: cancelling the base stops
    them too, before they spend anything more. Cancelling one variant stops only that variant."""
    owner, app, _estimate, builds = await family(client, idp, github, ["en", "de", "id"], cap=3_000_000)
    base, german, indonesian = builds
    rt = runtime_for(app, ScriptedProvider())
    assert (await drive(app, german, runtime=rt))[-1] == "waiting"
    alone = await unsafe(owner, "POST", f"/api/v1/builds/{indonesian}/cancel", json={})
    assert alone.status_code in (200, 202), alone.text
    assert (await owner.get(f"/api/v1/builds/{indonesian}")).json()["status"] == "cancelled"
    assert (await owner.get(f"/api/v1/builds/{base}")).json()["status"] in ("queued", "running")
    assert (await owner.get(f"/api/v1/builds/{german}")).json()["status"] in ("queued", "running")

    cancelled = await unsafe(owner, "POST", f"/api/v1/builds/{base}/cancel", json={"reason": "wrong branch"})
    assert cancelled.status_code in (200, 202), cancelled.text
    await drive(app, base, runtime=rt)
    await resume(app, german, rt)
    for build in (base, german):
        assert (await owner.get(f"/api/v1/builds/{build}")).json()["status"] == "cancelled", build
    requested = ddl(
        "SELECT payload FROM build_events WHERE build_id = :b AND event_type = 'build.cancel_requested'",
        b=german,
    )
    assert [r[0] for r in requested] == [{"reason": "wrong branch", "with_base": True}]
    assert (
        ddl(
            "SELECT count(*) FROM build_tasks WHERE build_id = ANY(CAST(:ids AS uuid[])) AND state IN ('pending', 'leased', 'retry_wait')",
            ids=builds,
        )[0][0]
        == 0
    )


async def test_a_variant_fails_when_its_base_stops_before_planning(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, app, _estimate, builds = await family(client, idp, github, ["en", "de"], cap=2_000_000)
    base, variant_build = builds
    rt = runtime_for(app, ScriptedProvider())
    assert (await drive(app, variant_build, runtime=rt))[-1] == "waiting"
    # The base fails on its own (not cancelled), before it has planned.
    ddl(
        "UPDATE build_tasks SET state = 'cancelled' WHERE build_id = :b AND state IN ('pending', 'retry_wait')",
        b=base,
    )
    ddl("UPDATE builds SET status = 'failed', completed_at = now() WHERE id = :b", b=base)
    await resume(app, variant_build, rt)
    status = (await owner.get(f"/api/v1/builds/{variant_build}")).json()
    assert status["status"] == "failed", status
    assert any(s["error_code"] == "base_unavailable" for s in status["stages"]), status["stages"]


def explanations(build: str) -> list[tuple[str, Any]]:
    return [
        (str(r[0]), r[1])
        for r in ddl(
            "SELECT source_file_id, chunks FROM file_explanations WHERE build_id = :b ORDER BY source_file_id",
            b=build,
        )
    ]


async def test_a_translated_language_reads_the_bases_text_instead_of_the_source(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    """P07: with `translate`, a further language translates the base's validated explanations, overview and
    pages (the replay translation returns them unchanged), costs the translation estimate, gets the cap
    share of that estimate, and is still checked against the source."""
    owner, app, estimate, builds = await family(
        client, idp, github, ["en", "de"], cap=2_000_000, translate=True
    )
    per_language, per_translation = estimate["per_language_micro_usd"], estimate["per_translation_micro_usd"]
    assert per_translation["high"] < per_language["high"] and per_translation["low"] < per_language["low"]
    assert estimate["cost_micro_usd"] == {k: per_language[k] + per_translation[k] for k in ("low", "high")}
    base, german = builds
    caps = dict(
        ddl(
            "SELECT id::text, reserved_micro_usd FROM builds WHERE id = ANY(CAST(:ids AS uuid[]))", ids=builds
        )
    )
    assert caps[german] < caps[base] and caps[german] + caps[base] == 2_000_000

    provider = ScriptedProvider()
    rt = runtime_for(app, provider)
    assert (await drive(app, german, runtime=rt))[-1] == "waiting"  # the base has not explained yet
    await drive(app, base, runtime=rt)
    written = list(provider.calls)
    assert "explain" in written and "translate" not in written, sorted(set(written))
    provider.calls.clear()
    for _ in range(4):  # each waiting stage resumes once the base has finished it
        await resume(app, german, rt)
        if (await owner.get(f"/api/v1/builds/{german}")).json()["status"] != "running":
            break
    status = (await owner.get(f"/api/v1/builds/{german}")).json()
    assert status["status"] == "ready", status
    assert "translate" in provider.calls
    assert not {"explain", "overview", "plan", "guide"} & set(provider.calls)  # nothing written from source
    # The translated pages' claims are checked against the source like any page's (here the replay
    # translation repeats the base's claims, so their checks come from the workspace's cache).
    checked = "SELECT status, count(*) FROM claim_checks WHERE build_id = :b AND current GROUP BY status ORDER BY status"
    assert ddl(checked, b=german) == ddl(checked, b=base) and ddl(checked, b=german)
    assert pages(german) == pages(base)
    ours, theirs = explanations(german), explanations(base)
    assert [(f, [(c["start_line"], c["summary"], c["notes"]) for c in chunks]) for f, chunks in ours] == [
        (f, [(c["start_line"], c["summary"], c["notes"]) for c in chunks]) for f, chunks in theirs
    ]
    assert all(c["provenance"]["translated_from"] == base for _f, chunks in ours for c in chunks)
    config = ddl("SELECT config->'generation' FROM builds WHERE id = :b", b=german)[0][0]
    assert config["translate"] is True and config["languages"] == ["de"] and config["variant_of"] == base


@dataclass
class PlanTranslator(ScriptedProvider):
    """Translates a guide plan's prose by marking it; `break_slugs` returns a plan with a changed slug."""

    break_slugs: bool = False

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        result = await super().generate(request)
        if request.purpose != "translate" or request.evidence.get("kind") != "plan":
            return result
        pages = [
            {
                **p,
                "slug": f"x-{p['slug']}" if self.break_slugs else p["slug"],
                "title": f"DE {p['title']}",
                "section": f"DE {p['section']}"[:60],
                "purpose": f"DE {p['purpose']}",
            }
            for p in result.output["pages"]
        ]
        return GenerationResult(
            {**result.output, "pages": pages}, result.usage, result.provider_request_id, result.model_id
        )


def purposes(build: str) -> list[tuple[str, str, str]]:
    return [
        (r[0], r[1], r[2])
        for r in ddl(
            "SELECT slug, section, purpose FROM guide_pages WHERE build_id = :b ORDER BY ordinal", b=build
        )
    ]


async def test_a_variant_in_another_language_gets_the_plan_in_its_language(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    """A language variant shows its guide's sections and purposes in its own language: the base's plan is
    translated once (P07) and only its prose changes; a level variant in the base's language copies it,
    and a translation that changes a slug or a range is refused, leaving the base's text."""
    owner, _ws, project, base, app = await ready_build(client, idp, github)
    theirs = purposes(base)
    assert theirs

    async def built(provider: PlanTranslator, language: str, level: str) -> str:
        estimate = await variant(owner, project, base, language=language, level=level, preset="balanced-v1")
        assert estimate.status_code == 201, estimate.text
        started = await start_from(owner, project, estimate.json()["id"], cap=2_000_000)
        assert started.status_code == 202, started.text
        await drive(app, started.json()["id"], runtime=runtime_for(app, provider))
        assert (await owner.get(f"/api/v1/builds/{started.json()['id']}")).json()["status"] == "ready"
        return str(started.json()["id"])

    translator = PlanTranslator()
    german = await built(translator, "de", "standard")
    assert translator.calls.count("translate") == 1 and "plan" not in translator.calls
    assert purposes(german) == [(slug, f"DE {s}"[:60], f"DE {p}") for slug, s, p in theirs]
    assert pages(german) == pages(base)  # the anchors are the base's
    plan = ddl(
        "SELECT a.object_key FROM artifacts a WHERE a.build_id = :b AND a.kind = 'guide_plan'", b=german
    )
    assert plan

    same_language = PlanTranslator()
    beginner = await built(same_language, "en", "beginner")
    assert "translate" not in same_language.calls and purposes(beginner) == theirs

    breaking = PlanTranslator(break_slugs=True)
    refused = await built(breaking, "fr", "standard")
    assert breaking.calls.count("translate") >= 1 and purposes(refused) == theirs


async def test_a_translation_of_a_ready_build_keeps_its_reader_level(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, project, base, app = await ready_build(client, idp, github)
    level = await make_estimate(
        owner,
        project,
        snapshot_of(base),
        preset="replay-v2",
        languages=["de"],
        levels=["beginner"],
        variant_of=base,
        translate=True,
    )
    assert level.status_code == 422 and code(level) == "TRANSLATION_NEEDS_LANGUAGE"
    alone = await make_estimate(owner, project, snapshot_of(base), preset="replay-v2", translate=True)
    assert alone.status_code == 422
    translated = await make_estimate(
        owner,
        project,
        snapshot_of(base),
        preset="replay-v2",
        languages=["id"],
        variant_of=base,
        translate=True,
    )
    assert translated.status_code == 201, translated.text
    started = await start_from(owner, project, translated.json()["id"])
    assert started.status_code == 202, started.text
    await drive(app, started.json()["id"], runtime=runtime_for(app))
    status = (await owner.get(f"/api/v1/builds/{started.json()['id']}")).json()
    assert status["status"] == "ready" and status["variant_of"] == base, status
    assert pages(started.json()["id"]) == pages(base)


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
