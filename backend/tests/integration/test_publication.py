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
    body.pop("public_slug", None)  # 0039: the address is the owner's handle and the project's slug
    return await unsafe(owner, "POST", f"/api/v1/projects/{project}/publication", json=body, headers=headers)


async def address(owner: httpx.AsyncClient, project: str) -> str:
    """A project's site address, "<owner>/<project>" (0039): the owner's handle and the project's slug."""
    detail = (await owner.get(f"/api/v1/projects/{project}")).json()
    workspace = (await owner.get(f"/api/v1/workspaces/{detail['workspace_id']}")).json()
    return f"{workspace['owner']['handle']}/{detail['slug']}"


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
    assert private.json()["visibility"] == "private"  # A15 default
    assert private.json()["address"] == await address(owner, project)  # 0039: /sites/<owner>/<project>
    etag = private.headers["etag"]
    current = await owner.get(f"/api/v1/projects/{project}/publication")
    assert current.headers["etag"] == etag and current.json()["build_id"] == build

    slug = await address(owner, project)
    unconfirmed = await publish(
        owner, project, etag, build_id=build, visibility="public", public_slug=slug, confirmation="api"
    )
    assert unconfirmed.status_code == 422 and unconfirmed.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
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
    slug = await address(owner, project)
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
    slug = await address(owner, project)
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
    slug = await address(owner, project)
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
    slug = await address(owner, project)
    private = await publish(owner, project, build_id=build, visibility="private", public_slug=slug)
    assert (
        private.status_code == 200 and private.json()["address"] == slug
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


async def test_sites_of_inactive_workspaces_or_deleted_projects_are_not_served(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, ws, project, build, app = await ready_build(client, idp, github)
    slug = await address(owner, project)
    assert (
        await publish(
            owner, project, build_id=build, visibility="public", public_slug=slug, confirmation="API"
        )
    ).status_code == 200
    async with browser(app) as visitor:
        assert (await visitor.get(f"/api/v1/published/{slug}")).status_code == 200
        ddl("UPDATE workspaces SET status = 'suspended' WHERE id = :w", w=ws)
        assert (await visitor.get(f"/api/v1/published/{slug}")).status_code == 404
        ddl("UPDATE workspaces SET status = 'active' WHERE id = :w", w=ws)
        assert (await visitor.get(f"/api/v1/published/{slug}")).status_code == 200
        ddl("UPDATE projects SET deleted_at = now() WHERE id = :p", p=project)
        assert (await visitor.get(f"/api/v1/published/{slug}/tree")).status_code == 404


async def test_an_operator_suspension_is_not_lifted_by_republishing(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, project, build, _app = await ready_build(client, idp, github)
    first = await publish(owner, project, build_id=build)
    ddl(
        "UPDATE publications SET suspended_at = now(), suspended_reason = 'operator' WHERE project_id = :p",
        p=project,
    )
    again = await publish(owner, project, first.headers["etag"], build_id=build)
    assert again.status_code == 409 and again.json()["error"]["code"] == "SUSPENDED_BY_OPERATOR"
    assert (
        ddl("SELECT suspended_reason FROM publications WHERE project_id = :p", p=project)[0][0] == "operator"
    )
    # Unpublishing would delete the takedown along with the row, letting a fresh publish skip it.
    withdraw = await unsafe(
        owner,
        "DELETE",
        f"/api/v1/projects/{project}/publication",
        headers={"If-Match": first.headers["etag"]},
    )
    assert withdraw.status_code == 409 and withdraw.json()["error"]["code"] == "SUSPENDED_BY_OPERATOR"
    import sqlalchemy as sa

    with pytest.raises(sa.exc.IntegrityError):  # the database refuses the delete too
        ddl("DELETE FROM publications WHERE project_id = :p", p=project)
    assert (
        ddl("SELECT suspended_reason FROM publications WHERE project_id = :p", p=project)[0][0] == "operator"
    )


async def test_a_concurrent_revoke_cannot_slip_past_the_access_check(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    """The publish reads the connection FOR SHARE: a revoke in flight makes it wait, then refuse."""
    import asyncio

    import sqlalchemy as sa

    from tests.integration.conftest import MIGRATION_URL, engine

    owner, _ws, project, build, _app = await ready_build(client, idp, github)
    connection = ddl(
        "SELECT r.connection_id FROM projects p JOIN repositories r ON r.id = p.repository_id WHERE p.id = :p",
        p=project,
    )[0][0]
    with engine(MIGRATION_URL).connect() as revoke:
        revoke.execute(
            sa.text("UPDATE repository_connections SET status = 'revoked' WHERE id = :c"), {"c": connection}
        )
        attempt = asyncio.create_task(publish(owner, project, build_id=build))
        await asyncio.sleep(0.5)
        assert not attempt.done(), "the publish must wait for the revoke in flight"
        revoke.commit()
    refused = await asyncio.wait_for(attempt, 10)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "CONNECTION_INACTIVE"
    assert ddl("SELECT count(*) FROM publications WHERE project_id = :p", p=project)[0][0] == 0


async def test_a_site_serves_the_ready_language_variants_of_its_build(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    """FR-026: a publish approves the ready language variants of the build's family, each through the review
    gate, and records them; the site serves exactly those, through `language`. A variant built after the
    publish waits for the next one; a variant that fails the gate, and a language the site lacks, are 404."""
    from tests.integration.test_budgets import make_estimate, start_from

    owner, _ws, project, base, app = await ready_build(client, idp, github)
    snapshot = str(ddl("SELECT snapshot_id FROM builds WHERE id = :b", b=base)[0][0])
    variants: dict[str, str] = {}

    async def build_variant(language: str) -> None:
        estimate = await make_estimate(
            owner, project, snapshot, preset="replay-v2", languages=[language], variant_of=base
        )
        started = await start_from(owner, project, estimate.json()["id"])
        assert started.status_code == 202, started.text
        variants[language] = started.json()["id"]
        await drive(app, variants[language], runtime=runtime_for(app))

    await build_variant("de")
    slug = await address(owner, project)
    published = await publish(owner, project, build_id=base, visibility="private", public_slug=slug)
    assert published.status_code == 200, published.text
    assert published.json()["variant_build_ids"] == [variants["de"]]
    await build_variant("id")  # built after the publish: not on the site yet
    assert [
        v["language"] for v in (await owner.get(f"/api/v1/published/{slug}/variants")).json()["items"]
    ] == [
        "en",
        "de",
    ]
    assert (await owner.get(f"/api/v1/published/{slug}/tree", params={"language": "id"})).status_code == 404
    etag = (await owner.get(f"/api/v1/projects/{project}/publication")).headers["etag"]
    republished = await publish(owner, project, etag, build_id=base, visibility="private", public_slug=slug)
    assert republished.status_code == 200, republished.text
    assert sorted(republished.json()["variant_build_ids"]) == sorted([variants["de"], variants["id"]])

    listed = (await owner.get(f"/api/v1/published/{slug}/variants")).json()["items"]
    assert [(v["language"], v["published"]) for v in listed] == [("en", True), ("de", False), ("id", False)]
    assert all(set(v) == {"language", "published"} for v in listed)  # no build IDs on a site
    page = ddl("SELECT slug FROM guide_pages WHERE build_id = :b ORDER BY ordinal LIMIT 1", b=base)[0][0]
    english = (await owner.get(f"/api/v1/published/{slug}/guide/{page}")).json()
    german = (await owner.get(f"/api/v1/published/{slug}/guide/{page}", params={"language": "de"})).json()
    expected = ddl(
        "SELECT title FROM guide_pages WHERE build_id = :b AND slug = :s", b=variants["de"], s=page
    )[0][0]
    assert german["title"] == expected and german["slug"] == english["slug"]  # the same anchor, in German
    assert (await owner.get(f"/api/v1/published/{slug}/tree", params={"language": "id"})).status_code == 200

    # A variant that no longer passes the review gate leaves the site; unknown languages are not served.
    ddl(
        "UPDATE claim_checks SET status = 'invalid', deterministic = 'invalid' WHERE build_id = :b",
        b=variants["id"],
    )
    listed = (await owner.get(f"/api/v1/published/{slug}/variants")).json()["items"]
    assert [v["language"] for v in listed] == ["en", "de"]
    assert (await owner.get(f"/api/v1/published/{slug}/tree", params={"language": "id"})).status_code == 404
    assert (await owner.get(f"/api/v1/published/{slug}/tree", params={"language": "ja"})).status_code == 404
    assert (await owner.get(f"/api/v1/published/{slug}/tree", params={"language": "tlh"})).status_code == 422


async def test_a_site_lives_under_its_owner_and_old_addresses_still_lead_there(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    """0039: /sites/<owner>/<project>. A renamed owner keeps answering at the old handle while it stays
    reserved for them; a pre-0039 one-segment address leads to the project's address, and its MCP
    endpoint keeps answering."""
    owner, _ws, project, build, app = await ready_build(client, idp, github)
    published = await publish(owner, project, build_id=build, visibility="public", confirmation="API")
    assert published.status_code == 200, published.text
    old = published.json()["address"]
    handle, slug = old.split("/")
    renamed = f"r{secrets.token_hex(4)}"
    assert (await unsafe(owner, "PUT", "/api/v1/me/handle", json={"handle": renamed})).status_code == 200
    async with browser(app) as visitor:
        at_old = (await visitor.get(f"/api/v1/published/{old}")).json()
        assert at_old["address"] == f"{renamed}/{slug}"  # served, and names where it lives now
        assert (await visitor.get(f"/api/v1/published/{renamed}/{slug}")).status_code == 200
        assert (await visitor.get(f"/api/v1/published/{renamed}/other")).status_code == 404
        alias = f"old-{secrets.token_hex(4)}"
        ddl("INSERT INTO site_aliases(alias, project_id) VALUES (:a, :p)", a=alias, p=project)
        assert (await visitor.get(f"/api/v1/site-aliases/{alias}")).json() == {
            "owner": renamed,
            "project": slug,
        }
        assert (await visitor.get(f"/api/v1/site-aliases/nope-{secrets.token_hex(3)}")).status_code == 404
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        legacy = await visitor.post(f"/api/v1/mcp/{alias}", json=ping, headers={"Accept": "application/json"})
        assert legacy.status_code == 200 and legacy.json()["result"] == {}
        ddl("UPDATE site_aliases SET expires_at = now() - interval '1 second' WHERE alias = :a", a=alias)
        assert (await visitor.get(f"/api/v1/site-aliases/{alias}")).status_code == 404
    # Nobody else can take the old handle while it is reserved, so the old address cannot change hands.
    assert (
        ddl(
            "SELECT count(*) FROM handle_reservations WHERE handle = :h AND namespace_id IS NOT NULL",
            h=handle,
        )[0][0]
        == 1
    )


async def test_an_old_address_of_a_private_site_leads_only_its_members_there(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, new_user: NewUser
) -> None:
    """0039 security review: an old members-only slug names its new address only to members; anyone
    else gets the same 404 as for an unknown one, and its MCP endpoint gives no hint without a token."""
    owner, _ws, project, build, app = await ready_build(client, idp, github)
    assert (await publish(owner, project, build_id=build)).status_code == 200  # private
    alias = f"old-{secrets.token_hex(4)}"
    ddl("INSERT INTO site_aliases(alias, project_id) VALUES (:a, :p)", a=alias, p=project)
    assert (await owner.get(f"/api/v1/site-aliases/{alias}")).json()["owner"]
    stranger = await new_user()
    assert (await stranger.get(f"/api/v1/site-aliases/{alias}")).status_code == 404
    async with browser(app) as visitor:
        assert (await visitor.get(f"/api/v1/site-aliases/{alias}")).status_code == 404
        legacy = await visitor.post(
            f"/api/v1/mcp/{alias}",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Accept": "application/json"},
        )
        assert legacy.status_code == 401 and "resource_metadata" not in legacy.headers["www-authenticate"]
