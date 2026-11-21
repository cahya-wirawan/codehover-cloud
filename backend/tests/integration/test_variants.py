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
from tests.integration.auth_support import FakeIdP, ddl
from tests.integration.conftest import needs_db
from tests.integration.test_budgets import make_estimate, start_from
from tests.integration.test_builds import drive
from tests.integration.test_generation import ScriptedProvider, runtime_for
from tests.integration.test_github import FakeGitHub, github  # noqa: F401
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
