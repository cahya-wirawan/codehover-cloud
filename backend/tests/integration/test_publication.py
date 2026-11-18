"""Publication pointer, public disclosure, rollback, suspension and delivery (T15; 06, 09, A15, A17).

M3 exit criterion: public opt-in and revocation. No real provider or GitHub is called.
"""

from __future__ import annotations

import secrets
from typing import Any

import httpx
import pytest

from app.engine.gateway import ProviderError
from tests.integration.auth_support import FakeIdP, browser, ddl, unsafe
from tests.integration.conftest import needs_db
from tests.integration.test_budgets import make_estimate, start_from
from tests.integration.test_builds import drive
from tests.integration.test_generation import ScriptedProvider, generation_build, runtime_for
from tests.integration.test_github import FakeGitHub, github  # noqa: F401
from tests.integration.test_quality import Opinionated
from tests.integration.test_workspaces import NewUser, join, new_user  # noqa: F401

pytestmark = [pytest.mark.integration, needs_db]


async def publish(
    owner: httpx.AsyncClient, project: str, if_match: str | None = None, **body: Any
) -> httpx.Response:
    headers = {"Idempotency-Key": secrets.token_hex(12)}
    if if_match:
        headers["If-Match"] = if_match
    return await unsafe(owner, "POST", f"/api/v1/projects/{project}/publication", json=body, headers=headers)


def project_of(build: str) -> str:
    return str(ddl("SELECT project_id FROM builds WHERE id = :b", b=build)[0][0])


async def ready_build(
    client: httpx.AsyncClient, idp: FakeIdP, gh: FakeGitHub, **kw: Any
) -> tuple[httpx.AsyncClient, str, str, str, Any]:
    owner, ws, build, app = await generation_build(client, idp, gh, preset="replay-v2", **kw)
    await drive(app, build, runtime=runtime_for(app))
    return owner, ws, project_of(build), build, app


async def second_build(
    owner: httpx.AsyncClient, app: Any, project: str, preset: str = "replay-v2", provider: Any = None
) -> str:
    snapshot = ddl("SELECT id FROM snapshots WHERE project_id = :p ORDER BY created_at LIMIT 1", p=project)[
        0
    ][0]
    estimate = (await make_estimate(owner, project, str(snapshot), preset=preset, guide_pages=3)).json()
    started = await start_from(
        owner, project, estimate["id"], cap=5_000_000 if preset != "replay-v2" else None
    )
    assert started.status_code == 202, started.text
    build = started.json()["id"]
    await drive(app, build, runtime=runtime_for(app, provider))
    return str(build)


async def test_private_by_default_then_public_with_confirmation(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, ws, project, build, app = await ready_build(client, idp, github)
    assert (await owner.get(f"/api/v1/projects/{project}/publication")).status_code == 404
    private = await publish(owner, project, build_id=build)
    assert private.status_code == 200, private.text
    assert private.json()["visibility"] == "private" and private.json()["public_slug"] is None  # A15 default
    etag = private.headers["etag"]
    current = await owner.get(f"/api/v1/projects/{project}/publication")
    assert current.headers["etag"] == etag and current.json()["build_id"] == build

    slug = f"site-{secrets.token_hex(3)}"
    unconfirmed = await publish(
        owner, project, etag, build_id=build, visibility="public", public_slug=slug, confirmation="api"
    )
    assert unconfirmed.status_code == 422 and unconfirmed.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
    bad_slug = await publish(
        owner, project, etag, build_id=build, visibility="public", public_slug="A!", confirmation="API"
    )
    assert bad_slug.status_code == 422
    public = await publish(
        owner, project, etag, build_id=build, visibility="public", public_slug=slug, confirmation="API"
    )
    assert public.status_code == 200, public.text
    assert public.json()["version"] == 2

    async with browser(app) as visitor:  # anonymous: no cookies at all
        site = await visitor.get(f"/api/v1/published/{slug}")
        assert (
            site.status_code == 200 and site.json()["project"] == "API" and site.json()["generated"] is True
        )
        tree = (await visitor.get(f"/api/v1/published/{slug}/tree")).json()["items"]
        src = next(e for e in tree if e["name"] == "src")
        files = (await visitor.get(f"/api/v1/published/{slug}/tree", params={"path": src["path"]})).json()[
            "items"
        ]
        app_py = next(f for f in files if f["name"] == "app.py")
        source = await visitor.get(f"/api/v1/published/{slug}/files/{app_py['file_id']}")
        assert source.status_code == 200 and source.json()["lines"][0].startswith('"""Tiny app')
        guide = (await visitor.get(f"/api/v1/published/{slug}/guide")).json()
        assert guide["pages"]
        page = await visitor.get(f"/api/v1/published/{slug}/guide/{guide['pages'][0]['slug']}")
        assert page.status_code == 200 and page.json()["claims"]
        hits = (await visitor.get(f"/api/v1/published/{slug}/search", params={"q": "helper"})).json()["items"]
        assert hits and hits[0]["name"] == "helper"
        for body in (site.text, source.text, page.text):
            assert build not in body  # the build ID never leaves the resolver (08)
        assert (await visitor.get(f"/api/v1/builds/{build}")).status_code == 401  # build routes stay private
        assert (await visitor.get(f"/api/v1/published/no-such-{secrets.token_hex(2)}")).status_code == 404
    changes = ddl(
        "SELECT action, metadata FROM audit_events WHERE workspace_id = :w AND action LIKE 'publication.%' ORDER BY created_at",
        w=ws,
    )
    assert [c[0] for c in changes] == ["publication.changed", "publication.changed"]
    assert changes[1][1]["visibility"] == "public" and changes[1][1]["previous_visibility"] == "private"


async def test_etag_concurrency_rollback_and_unpublish(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, ws, project, first, app = await ready_build(client, idp, github)
    slug = f"site-{secrets.token_hex(3)}"
    v1 = await publish(
        owner, project, build_id=first, visibility="public", public_slug=slug, confirmation="API"
    )
    assert v1.status_code == 200
    second = await second_build(owner, app, project)
    missing = await publish(
        owner, project, build_id=second, visibility="public", public_slug=slug, confirmation="API"
    )
    assert missing.status_code == 428 and missing.json()["error"]["code"] == "PRECONDITION_REQUIRED"
    v2 = await publish(
        owner,
        project,
        v1.headers["etag"],
        build_id=second,
        visibility="public",
        public_slug=slug,
        confirmation="API",
    )
    assert v2.status_code == 200 and v2.json()["build_id"] == second
    stale = await publish(
        owner,
        project,
        v1.headers["etag"],
        build_id=first,
        visibility="public",
        public_slug=slug,
        confirmation="API",
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "VERSION_CONFLICT"
    rollback = await publish(
        owner,
        project,
        v2.headers["etag"],
        build_id=first,
        visibility="public",
        public_slug=slug,
        confirmation="API",
    )
    assert (
        rollback.status_code == 200
        and rollback.json()["build_id"] == first
        and rollback.json()["version"] == 3
    )

    no_etag = await unsafe(owner, "DELETE", f"/api/v1/projects/{project}/publication")
    assert no_etag.status_code == 428
    removed = await unsafe(
        owner,
        "DELETE",
        f"/api/v1/projects/{project}/publication",
        headers={"If-Match": rollback.headers["etag"]},
    )
    assert removed.status_code == 204
    async with browser(app) as visitor:
        assert (await visitor.get(f"/api/v1/published/{slug}")).status_code == 404  # stops at once
    assert (
        ddl(
            "SELECT count(*) FROM audit_events WHERE workspace_id = :w AND action = 'publication.removed'",
            w=ws,
        )[0][0]
        == 1
    )


async def test_the_review_gate_and_roles(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, new_user: NewUser
) -> None:
    owner, ws, project, build, app = await ready_build(client, idp, github)
    outage = ScriptedProvider(
        faults={"check": [ProviderError("transient", "provider_unavailable") for _ in range(200)]}
    )
    unchecked = await second_build(owner, app, project, preset="balanced-v1", provider=outage)
    blocked = await publish(owner, project, build_id=unchecked)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "PUBLICATION_BLOCKED"
    assert "unchecked_claims" in blocked.json()["error"]["details"]["blockers"]

    partial = await second_build(owner, app, project, preset="balanced-v1", provider=_partial_checker())
    needs_ack = await publish(owner, project, build_id=partial)
    assert needs_ack.status_code == 422 and needs_ack.json()["error"]["code"] == "ACKNOWLEDGMENT_REQUIRED"
    acknowledged = await publish(owner, project, build_id=partial, acknowledge_partial=True)
    assert acknowledged.status_code == 200 and acknowledged.json()["acknowledged_partial"] is True

    editor = await new_user()
    await join(owner, ws, editor, "editor")
    refused = await publish(editor, project, acknowledged.headers["etag"], build_id=build)
    assert refused.status_code == 403  # publishing is an admin action (09)
    assert (await editor.get(f"/api/v1/projects/{project}/publication")).status_code == 200  # members read it

    stranger, _sws, foreign_build, _app = await generation_build(client, idp, github, preset="replay-v2")
    await drive(app, foreign_build, runtime=runtime_for(app))
    cross = await publish(owner, project, acknowledged.headers["etag"], build_id=foreign_build)
    assert cross.status_code == 404  # another workspace's build is invisible, never publishable here
    assert (await stranger.get(f"/api/v1/projects/{project}/publication")).status_code == 404


def _partial_checker() -> Any:
    class Partial(Opinionated):
        async def generate(self, request: Any) -> Any:
            if request.purpose == "check":
                from app.engine.gateway import GenerationResult, Usage

                checks = [
                    {
                        "claim_id": c["claim_id"],
                        "status": "partial" if i == 0 else "supported",
                        "reason": "t",
                        "evidence_line_numbers": [],
                    }
                    for i, c in enumerate(request.evidence["claims"])
                ]
                return GenerationResult({"checks": checks}, Usage(10, 5), "req", request.model.id)
            if request.purpose == "claim_repair":
                raise ProviderError("permanent", "provider_refusal")
            return await super().generate(request)

    return Partial()


async def test_revoked_access_suspends_delivery_until_republished(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    """A17 / FR-027: removing the installation suspends the site at once; publishing again needs access."""
    owner, ws, project, build, app = await ready_build(client, idp, github)
    slug = f"site-{secrets.token_hex(3)}"
    published = await publish(
        owner, project, build_id=build, visibility="public", public_slug=slug, confirmation="API"
    )
    assert published.status_code == 200
    connection = ddl(
        "SELECT r.connection_id FROM projects p JOIN repositories r ON r.id = p.repository_id WHERE p.id = :p",
        p=project,
    )[0][0]
    revoked = await unsafe(owner, "DELETE", f"/api/v1/workspaces/{ws}/connections/{connection}")
    assert revoked.status_code == 204
    async with browser(app) as visitor:
        assert (await visitor.get(f"/api/v1/published/{slug}")).status_code == 404
        assert (await visitor.get(f"/api/v1/published/{slug}/tree")).status_code == 404
    state = (await owner.get(f"/api/v1/projects/{project}/publication")).json()
    assert state["suspended_reason"] == "connection_inactive" and state["suspended_at"]
    again = await publish(
        owner,
        project,
        f'"publication-{state["version"]}"',
        build_id=build,
        visibility="public",
        public_slug=slug,
        confirmation="API",
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "CONNECTION_INACTIVE"


async def test_repository_revocation_also_suspends(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, project, build, app = await ready_build(client, idp, github)
    slug = f"site-{secrets.token_hex(3)}"
    assert (
        await publish(
            owner, project, build_id=build, visibility="public", public_slug=slug, confirmation="API"
        )
    ).status_code == 200
    ddl(
        "UPDATE repositories SET access_revoked_at = now() WHERE id = (SELECT repository_id FROM projects WHERE id = :p)",
        p=project,
    )
    async with browser(app) as visitor:
        assert (await visitor.get(f"/api/v1/published/{slug}")).status_code == 404
    assert (await owner.get(f"/api/v1/projects/{project}/publication")).json()[
        "suspended_reason"
    ] == "repository_revoked"


async def test_a_private_site_address_is_for_members_only(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, new_user: NewUser
) -> None:
    owner, ws, project, build, app = await ready_build(client, idp, github)
    slug = f"team-{secrets.token_hex(3)}"
    private = await publish(owner, project, build_id=build, visibility="private", public_slug=slug)
    assert (
        private.status_code == 200 and private.json()["public_slug"] == slug
    )  # no disclosure confirmation needed
    assert (await owner.get(f"/api/v1/published/{slug}")).status_code == 200  # a member reads it
    viewer = await new_user()
    await join(owner, ws, viewer, "viewer")
    assert (await viewer.get(f"/api/v1/published/{slug}/tree")).status_code == 200
    outsider = await new_user()  # signed in, but not a member
    assert (await outsider.get(f"/api/v1/published/{slug}")).status_code == 404
    async with browser(app) as visitor:
        assert (await visitor.get(f"/api/v1/published/{slug}")).status_code == 404
        assert (await visitor.get(f"/api/v1/published/{slug}/tree")).status_code == 404
