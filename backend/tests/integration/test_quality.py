"""Deterministic citations, the independent checker, repair, waivers and the gate (T12; 07, A13, A14).

The checker sees only claims and their exact cited lines. A scripted provider injects checker outages,
unsupported labels and malformed citations; the replay gateway answers everything else. No real
provider is called.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from app.engine import generate
from app.engine.gateway import GenerationRequest, GenerationResult, ProviderError, Usage
from tests.integration.auth_support import FakeIdP, ddl, unsafe
from tests.integration.conftest import needs_db
from tests.integration.test_builds import drive, setup_project
from tests.integration.test_generation import ScriptedProvider, generation_build, runtime_for
from tests.integration.test_github import FakeGitHub, github  # noqa: F401

pytestmark = [pytest.mark.integration, needs_db]


def current_claims(build: str) -> list[Any]:
    return ddl(
        "SELECT claim_key, claim_text, status, deterministic, reason, revision FROM claim_checks"
        " WHERE build_id = :b AND current ORDER BY ordinal",
        b=build,
    )


async def quality(owner: httpx.AsyncClient, build: str) -> dict[str, Any]:
    response = await owner.get(f"/api/v1/builds/{build}/quality")
    assert response.status_code == 200, response.text
    return dict(response.json())


async def test_replay_guide_is_fully_supported_and_publishable(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, build, app = await generation_build(client, idp, github, preset="replay-v2")
    assert await drive(app, build, runtime=runtime_for(app)) == ["completed"] * 8
    report = await quality(owner, build)
    assert report["claims"] > 0 and report["counts"]["supported"] == report["claims"], report
    assert report["publishable"] and report["blockers"] == [] and report["supported_fraction"] == 1.0
    rows = current_claims(build)
    assert all(r[3] == "valid" and r[5] == 0 for r in rows)
    slug = ddl("SELECT slug FROM guide_pages WHERE build_id = :b ORDER BY ordinal LIMIT 1", b=build)[0][0]
    page = (await owner.get(f"/api/v1/builds/{build}/guide/{slug}")).json()
    assert page["check_status"] == "supported" and page["claims"]
    assert all(c["status"] == "supported" and c["citations"] for c in page["claims"])
    listing = (await owner.get(f"/api/v1/builds/{build}/quality/checks", params={"limit": 2})).json()
    assert len(listing["items"]) == 2 and listing["next_cursor"]
    index = (await owner.get(f"/api/v1/builds/{build}/guide")).json()
    assert index["pages"] and all(p["section"] == "Code walkthrough" for p in index["pages"])
    symbols = [r[0] for r in ddl("SELECT id FROM symbols WHERE build_id = :b", b=build)]
    cards = [(await owner.get(f"/api/v1/builds/{build}/symbols/{sid}")).json() for sid in symbols]
    linked = [c for c in cards if c["guides"]]
    assert linked, "a definition inside cited lines links back to its guide page"
    assert all({g["slug"] for g in c["guides"]} <= {p["slug"] for p in index["pages"]} for c in linked)


async def test_checker_outage_leaves_claims_unchecked_and_blocks_publication(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    """A13: the build still finishes as a ready draft; claims are unchecked, never labeled supported."""
    owner, _ws, build, app = await generation_build(client, idp, github, preset="balanced-v1", cap=5_000_000)
    outage = [ProviderError("transient", "provider_unavailable") for _ in range(200)]
    provider = ScriptedProvider(faults={"check": outage})
    assert await drive(app, build, runtime=runtime_for(app, provider)) == ["completed"] * 8
    assert (await owner.get(f"/api/v1/builds/{build}")).json()["status"] == "ready"
    report = await quality(owner, build)
    assert report["counts"]["unchecked"] == report["claims"] > 0
    assert report["counts"]["supported"] == 0
    assert not report["publishable"] and "unchecked_claims" in report["blockers"]
    assert all(r[2] == "unchecked" and "checker unavailable" in r[4] for r in current_claims(build))


@dataclass
class Opinionated(ScriptedProvider):
    """The checker calls the first claim of every page unsupported; `guide_suffix` is appended to pages."""

    guide_suffix: str = ""
    repair: bool = True
    checked: list[list[str]] = field(default_factory=list)

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        if request.purpose == "check":
            self.calls.append("check")
            claims = request.evidence["claims"]
            self.checked.append([c["claim_id"] for c in claims])
            checks = [
                {
                    "claim_id": c["claim_id"],
                    "status": "unsupported" if i == 0 else "supported",
                    "reason": "test",
                    "evidence_line_numbers": [],
                }
                for i, c in enumerate(claims)
            ]
            return GenerationResult({"checks": checks}, Usage(100, 50), "req_check", request.model.id)
        if request.purpose == "claim_repair" and not self.repair:
            raise ProviderError("permanent", "provider_refusal", usage=Usage(10, 0))
        result = await super().generate(request)
        if request.purpose == "guide" and self.guide_suffix:
            return GenerationResult(
                {"markdown": result.output["markdown"] + self.guide_suffix},
                result.usage,
                result.provider_request_id,
                result.model_id,
            )
        return result


async def test_repair_removes_unsupported_claims_and_keeps_the_original(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, build, app = await generation_build(client, idp, github, preset="balanced-v1", cap=5_000_000)
    provider = Opinionated()
    assert await drive(app, build, runtime=runtime_for(app, provider)) == ["completed"] * 8
    pages = ddl(
        "SELECT id, markdown_key, original_markdown_key FROM guide_pages WHERE build_id = :b", b=build
    )
    assert pages and all(p[2] is not None and p[1] != p[2] for p in pages)
    objects = app.state.objects.objects
    for page_id, key, original in pages:
        assert len(objects[key][0]) < len(objects[original][0])  # the unsupported sentence is gone
        old = ddl(
            "SELECT count(*) FROM claim_checks WHERE guide_page_id = :p AND revision = 0 AND NOT current",
            p=page_id,
        )[0][0]
        new = ddl(
            "SELECT count(*) FROM claim_checks WHERE guide_page_id = :p AND revision = 1 AND current",
            p=page_id,
        )[0][0]
        assert old > 0  # original evidence kept beside the repaired page (19 P06)
        if new == 0:  # the page's only claim was removed: it has nothing checked left and blocks
            assert ddl("SELECT check_status FROM guide_pages WHERE id = :p", p=page_id)[0][0] == "unchecked"
    report = await quality(owner, build)
    if any(
        ddl("SELECT count(*) FROM claim_checks WHERE guide_page_id = :p AND current", p=p[0])[0][0] == 0
        for p in pages
    ):
        assert "uncited_pages" in report["blockers"]
    assert "claim_repair" in provider.calls
    # Unchanged claims keep their result; only changed ones go back to the checker (one repair cycle).
    assert provider.calls.count("check") <= 2 * len(pages)


async def test_unsupported_claims_block_until_waived_and_waivers_are_audited(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, ws, build, app = await generation_build(client, idp, github, preset="balanced-v1", cap=5_000_000)
    assert await drive(app, build, runtime=runtime_for(app, Opinionated(repair=False))) == ["completed"] * 8
    report = await quality(owner, build)
    assert report["counts"]["unsupported"] > 0 and "unsupported_claims" in report["blockers"]
    unsupported = [r[0] for r in current_claims(build) if r[2] == "unsupported"]
    supported = [r[0] for r in current_claims(build) if r[2] == "supported"]
    refused = await unsafe(
        owner,
        "POST",
        f"/api/v1/builds/{build}/quality/waivers",
        json={"claim_keys": supported[:1], "reason": "not needed here"},
    )
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "WAIVER_NOT_ALLOWED"
    short = await unsafe(
        owner,
        "POST",
        f"/api/v1/builds/{build}/quality/waivers",
        json={"claim_keys": unsupported, "reason": "ok"},
    )
    assert short.status_code == 422  # a waiver needs a real reason
    waived = await unsafe(
        owner,
        "POST",
        f"/api/v1/builds/{build}/quality/waivers",
        json={
            "claim_keys": unsupported,
            "reason": "Reviewed by the maintainer: the behavior is documented elsewhere.",
        },
    )
    assert waived.status_code == 200, waived.text
    report = await quality(owner, build)
    assert report["waived"]["unsupported"] == len(unsupported) and report["publishable"]
    assert report["counts"]["unsupported"] == len(unsupported)  # waived claims stay visible and counted
    audit = ddl(
        "SELECT metadata FROM audit_events WHERE workspace_id = :w AND action = 'claims.waived'", w=ws
    )
    assert audit and sorted(audit[0][0]["claim_keys"]) == sorted(unsupported)


@dataclass
class Rewriting(ScriptedProvider):
    """Rewrites every guide page the writer returns (not its repairs)."""

    rewrite: Callable[[str], str] = lambda markdown: markdown

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        result = await super().generate(request)
        if request.purpose != "guide":
            return result
        output = {"markdown": self.rewrite(result.output["markdown"])}
        return GenerationResult(output, result.usage, result.provider_request_id, result.model_id)


async def guide_markdown(owner: httpx.AsyncClient, build: str) -> list[str]:
    slugs = [r[0] for r in ddl("SELECT slug FROM guide_pages WHERE build_id = :b ORDER BY ordinal", b=build)]
    return [(await owner.get(f"/api/v1/builds/{build}/guide/{slug}")).json()["markdown"] for slug in slugs]


async def test_single_line_citations_are_stored_as_ranges_and_stay_valid(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    """Models write `[[file_id:N]]` for one line; the page is stored with `[[file_id:N-N]]`, without a
    repair call, and its claims are deterministically valid."""
    owner, _ws, build, app = await generation_build(client, idp, github, preset="balanced-v1", cap=5_000_000)
    provider = Rewriting(rewrite=lambda md: re.sub(r"\[\[([0-9a-f-]{36}):(\d+)-\d+\]\]", r"[[\1:\2]]", md))
    assert await drive(app, build, runtime=runtime_for(app, provider)) == ["completed"] * 8
    assert "repair" not in provider.calls
    pages = await guide_markdown(owner, build)
    assert pages and all(re.search(r"\[\[[0-9a-f-]{36}:(\d+)-\1\]\]", md) for md in pages)
    assert not any(re.search(r"\[\[[0-9a-f-]{36}:\d+\]\]", md) for md in pages)
    rows = current_claims(build)
    assert rows and all(r[3] == "valid" for r in rows)


async def test_a_malformed_citation_is_repaired_before_the_page_is_stored(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, build, app = await generation_build(client, idp, github, preset="balanced-v1", cap=5_000_000)
    provider = Rewriting(rewrite=lambda md: md + "\n\nThe entry point reads arguments [[src/app.py:5-7]].\n")
    assert await drive(app, build, runtime=runtime_for(app, provider)) == ["completed"] * 8
    assert "repair" in provider.calls
    assert any(
        "[[src/app.py:5-7]]" in prompt and "is not [[file_id:start-end]]" in prompt
        for prompt in provider.prompts
    )
    assert not any("src/app.py:5-7" in md for md in await guide_markdown(owner, build))
    assert not [r for r in current_claims(build) if r[2] == "invalid"]


def more_claims(markdown: str) -> str:
    """Eight more cited sentences, citing the page's first range."""
    first = re.search(r"\[\[[0-9a-f-]{36}:\d+-\d+\]\]", markdown)
    assert first
    return markdown + "".join(f"\n\nStep {i} happens in these lines {first.group(0)}." for i in range(8))


@dataclass
class Truncating(Rewriting):
    """The checker's answer is cut off at its output limit when a call has more than `fits` claims."""

    fits: int = 2
    sizes: list[int] = field(default_factory=list)

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        if request.purpose == "check":
            self.sizes.append(len(request.evidence["claims"]))
            if len(request.evidence["claims"]) > self.fits:
                self.calls.append("check")
                raise ProviderError("invalid", "output_truncated", usage=Usage(1000, 4096))
        return await super().generate(request)


async def test_a_cut_off_check_is_split_instead_of_repaired(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    """A truncated checker answer is not repaired (that asks the same task under the same limit); the
    batch is checked in halves, at most twice, and every claim gets the checker's answer."""
    owner, _ws, build, app = await generation_build(client, idp, github, preset="balanced-v1", cap=5_000_000)
    provider = Truncating(fits=2)
    assert await drive(app, build, runtime=runtime_for(app, provider)) == ["completed"] * 8
    assert max(provider.sizes) > 2 and max(provider.sizes) <= 8, (
        provider.sizes
    )  # the replay pages fit 2 splits
    repairs = ddl(
        "SELECT count(*) FROM provider_attempts WHERE build_id = :b AND purpose = 'repair' AND role = 'checker'",
        b=build,
    )[0][0]
    assert repairs == 0
    rows = current_claims(build)
    assert rows and all(r[2] == "supported" for r in rows), rows
    assert (await quality(owner, build))["blockers"] == []


async def test_splitting_a_cut_off_check_stops_after_two_levels(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    _owner, _ws, build, app = await generation_build(client, idp, github, preset="balanced-v1", cap=5_000_000)
    provider = Truncating(fits=0, rewrite=more_claims)  # every answer is cut off; pages have 8+ claims
    assert await drive(app, build, runtime=runtime_for(app, provider)) == ["completed"] * 8
    pages = ddl("SELECT count(DISTINCT guide_page_id) FROM claim_checks WHERE build_id = :b", b=build)[0][0]
    assert len(provider.sizes) == 7 * pages  # per page: the batch, its halves and their halves
    assert min(provider.sizes) >= 2  # quarters of 8+ claims are not split again
    rows = current_claims(build)
    assert rows and all(r[2] == "unchecked" and r[4] == "checker unavailable: output_truncated" for r in rows)


async def test_malformed_citations_are_invalid_and_cannot_be_waived(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A14: a citation by path instead of immutable file ID fails deterministic validation; it blocks
    publication and no waiver can lift it. The guide stage now repairs such pages before storing them, so
    this backstop is reached here by skipping that check (as for a page stored before it existed)."""
    monkeypatch.setattr(generate, "validate_guide", lambda output, evidence, pages=frozenset(): [])
    owner, _ws, build, app = await generation_build(client, idp, github, preset="balanced-v1", cap=5_000_000)
    provider = Opinionated(
        guide_suffix="\n\nThe entry point reads arguments [[src/app.py:5-7]].\n", repair=False
    )
    assert await drive(app, build, runtime=runtime_for(app, provider)) == ["completed"] * 8
    invalid = [r for r in current_claims(build) if r[2] == "invalid"]
    assert invalid and all(r[3] == "invalid" and r[4] == "citation_syntax" for r in invalid)
    assert all(
        not any(c == invalid[0][0] for c in batch) for batch in provider.checked
    )  # never sent to the checker
    report = await quality(owner, build)
    assert "invalid_citations" in report["blockers"]
    refused = await unsafe(
        owner,
        "POST",
        f"/api/v1/builds/{build}/quality/waivers",
        json={"claim_keys": [invalid[0][0]], "reason": "Trying to waive an invalid citation."},
    )
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "WAIVER_NOT_ALLOWED"
    with pytest.raises(sa.exc.IntegrityError):  # the database CHECK refuses it as well
        ddl(
            "UPDATE claim_checks SET waived_at = now(), waived_by = (SELECT created_by FROM builds WHERE id = :b),"
            " waived_reason = 'direct database waiver' WHERE build_id = :b AND status = 'invalid'",
            b=build,
        )


async def test_quality_is_private_and_waivers_need_an_admin(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    _owner, _ws, build, app = await generation_build(client, idp, github, preset="replay-v2")
    await drive(app, build, runtime=runtime_for(app))
    stranger, _, _ = await setup_project(client, idp, github)
    for path in (f"/api/v1/builds/{build}/quality", f"/api/v1/builds/{build}/quality/checks"):
        assert (await stranger.get(path)).status_code == 404
    key = current_claims(build)[0][0]
    response = await unsafe(
        stranger,
        "POST",
        f"/api/v1/builds/{build}/quality/waivers",
        json={"claim_keys": [key], "reason": "x" * 20},
    )
    assert response.status_code == 404
