"""Threat tests (M4 exit: zero critical isolation or XSS issues; 13 "API integration", "security").

The isolation sweep drives every route of the OpenAPI contract with the real identifiers of another
tenant: a signed-in stranger and an anonymous visitor are refused everywhere, learn nothing of the
victim, and change nothing. A new route the sweep does not know fails the test until it is classified.
"""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.integration.auth_support import FakeIdP, browser, ddl, unsafe
from tests.integration.conftest import needs_db
from tests.integration.test_builds import drive, setup_project
from tests.integration.test_generation import runtime_for
from tests.integration.test_github import FakeGitHub, github  # noqa: F401
from tests.integration.test_publication import publish, ready_build
from tests.integration.test_workspaces import NewUser, invite, new_user  # noqa: F401

pytestmark = [pytest.mark.integration, needs_db]
SPEC = json.loads((Path(__file__).resolve().parents[3] / "contracts" / "openapi.json").read_text())

# Not tenant-scoped, or scoped to the caller only: they cannot reach another tenant's data by ID.
UNSCOPED = {
    ("GET", "/api/v1/auth/csrf"),
    ("POST", "/api/v1/auth/logout"),
    ("GET", "/api/v1/auth/{provider}/callback"),
    ("GET", "/api/v1/auth/{provider}/start"),
    ("GET", "/api/v1/catalog/presets"),
    ("GET", "/api/v1/health/live"),
    ("GET", "/api/v1/health/ready"),
    ("GET", "/api/v1/integrations/github/callback"),
    ("GET", "/api/v1/integrations/github/setup"),
    ("POST", "/api/v1/invitations/accept"),
    ("GET", "/api/v1/me"),
    ("POST", "/api/v1/me/deletion"),
    ("POST", "/api/v1/me/export"),
    ("POST", "/api/v1/me/identities/{provider}/link"),
    ("GET", "/api/v1/me/sessions"),
    ("POST", "/api/v1/webhooks/github"),
    ("POST", "/api/v1/webhooks/stripe"),
    ("GET", "/api/v1/meta"),
    ("GET", "/api/v1/workspaces"),
    ("POST", "/api/v1/workspaces"),
    ("GET", "/api/v1/ops/summary"),
    ("GET", "/api/v1/ops/events"),
    ("POST", "/api/v1/ops/builds-paused"),
    ("PUT", "/api/v1/me/handle"),  # 0038: the caller's own handle
    ("GET", "/api/v1/handles/{handle}"),  # availability of a public name
    ("GET", "/api/v1/organizations"),  # the caller's own organizations and invitations
    ("POST", "/api/v1/organizations"),
}


async def victim(client: httpx.AsyncClient, idp: FakeIdP, gh: FakeGitHub) -> dict[str, Any]:
    owner, ws, project, build, app = await ready_build(client, idp, gh)
    slug = f"secret-{secrets.token_hex(3)}"
    assert (await publish(owner, project, build_id=build, public_slug=slug)).status_code == 200  # private
    export = await unsafe(
        owner,
        "POST",
        f"/api/v1/builds/{build}/exports",
        json={"format": "kb_json"},
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )
    await drive(app, build, runtime=runtime_for(app))
    invitation = await invite(owner, ws, "someone@example.test", "viewer")
    grant = await unsafe(
        owner, "POST", f"/api/v1/workspaces/{ws}/support-access", json={"hours": 1, "reason": "victim grant"}
    )
    token = await unsafe(
        owner,
        "POST",
        f"/api/v1/workspaces/{ws}/access-tokens",
        json={"name": "victim token", "expires_in_days": 30},
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )
    me = (await owner.get("/api/v1/me")).json()
    one = lambda sql, **kw: str(ddl(sql, **kw)[0][0])  # noqa: E731
    # 0038: an organization the victim owns, with a pending invitation of someone else.
    organization = await unsafe(
        owner,
        "POST",
        "/api/v1/organizations",
        json={"name": "Victim org", "handle": f"v{secrets.token_hex(4)}"},
    )
    assert organization.status_code == 201, organization.text
    # MCP sign-in (0037): a grant the owner gave a client, and a sign-in request still waiting for them.
    client_row = one(
        "INSERT INTO oauth_clients(client_id, client_name, redirect_uris)"
        " VALUES (:c, 'victim client', ARRAY['https://x.test/cb']) RETURNING id",
        c="chc_" + secrets.token_urlsafe(24),
    )
    request_id = one(
        "INSERT INTO oauth_authorizations(client, redirect_uri, code_challenge, resource, slug)"
        " VALUES (:c, 'https://x.test/cb', :ch, 'r', :s) RETURNING id",
        c=client_row,
        ch="A" * 43,
        s=slug,
    )
    oauth_grant = one(
        "INSERT INTO oauth_grants(workspace_id, user_id, project_id, client, client_name, authorization_id,"
        " refresh_hash, refresh_expires_at) VALUES (:w, :u, :p, :c, 'victim client', :a, :h, now() + interval '1 day')"
        " RETURNING id",
        w=ws,
        u=me["id"],
        p=project,
        c=client_row,
        a=request_id,
        h=secrets.token_bytes(32),
    )
    ids = {
        "workspace_id": ws,
        "project_id": project,
        "build_id": build,
        "export_id": export.json()["id"],
        "snapshot_id": one("SELECT snapshot_id FROM builds WHERE id = :b", b=build),
        "estimate_id": one("SELECT estimate_id FROM builds WHERE id = :b", b=build),
        "file_id": one(
            "SELECT id FROM source_files WHERE snapshot_id = (SELECT snapshot_id FROM builds WHERE id = :b) AND scan_status = 'selected' LIMIT 1",
            b=build,
        ),
        "symbol_id": one("SELECT id FROM symbols WHERE build_id = :b LIMIT 1", b=build),
        "slug": slug,
        "page": one("SELECT slug FROM guide_pages WHERE build_id = :b LIMIT 1", b=build),
        "connection_id": one("SELECT id FROM repository_connections WHERE workspace_id = :w", w=ws),
        "repository_id": one("SELECT repository_id FROM projects WHERE id = :p", p=project),
        "invitation_id": invitation.json()["id"],
        "user_id": me["id"],
        "grant_id": grant.json()["id"],
        "token_id": token.json()["id"],
        "oauth_grant_id": oauth_grant,
        "organization_id": organization.json()["id"],
        "request_id": request_id,
        "session_id": one(
            "SELECT id FROM sessions WHERE user_id = :u AND revoked_at IS NULL LIMIT 1", u=me["id"]
        ),
        "identity_id": me["identities"][0]["id"],
        "provider": "google",
    }
    return {
        "owner": owner,
        "app": app,
        "ids": ids,
        "workspace_slug": (await owner.get(f"/api/v1/workspaces/{ws}")).json()["slug"],
    }


def bodies(ids: dict[str, str], workspace_slug: str) -> dict[tuple[str, str], dict[str, Any]]:
    """A valid body per unsafe route, so authorization (not validation) decides."""
    return {
        ("POST", "/api/v1/builds/{build_id}/cancel"): {},
        ("PATCH", "/api/v1/organizations/{organization_id}"): {"name": "Hijacked"},
        ("POST", "/api/v1/organization-invitations/{invitation_id}/accept"): {},
        ("POST", "/api/v1/organization-invitations/{invitation_id}/decline"): {},
        ("PUT", "/api/v1/organizations/{organization_id}/handle"): {
            "handle": f"x{ids['organization_id'][:8]}"
        },
        ("PATCH", "/api/v1/organizations/{organization_id}/members/{user_id}"): {"role": "member"},
        ("POST", "/api/v1/organizations/{organization_id}/invitations"): {
            "handle": "anyone",
            "role": "owner",
        },
        ("POST", "/api/v1/builds/{build_id}/exports"): {"format": "kb_json"},
        ("POST", "/api/v1/builds/{build_id}/quality/waivers"): {
            "claim_keys": ["0" * 32],
            "reason": "trust me, it is fine",
        },
        ("POST", "/api/v1/builds/{build_id}/retry"): {},
        ("POST", "/api/v1/oauth/requests/{request_id}/decision"): {"approve": True},
        ("POST", "/api/v1/projects/{project_id}/builds"): {},
        ("POST", "/api/v1/projects/{project_id}/deletion"): {"confirmation": "API"},
        ("POST", "/api/v1/projects/{project_id}/estimates"): {
            "snapshot_id": ids["snapshot_id"],
            "config": {"model_preset": "replay-v2"},
        },
        ("POST", "/api/v1/projects/{project_id}/publication"): {
            "build_id": ids["build_id"],
            "visibility": "public",
            "public_slug": f"stolen-{secrets.token_hex(3)}",
            "confirmation": "API",
        },
        ("POST", "/api/v1/projects/{project_id}/snapshots"): {},
        ("PUT", "/api/v1/projects/{project_id}/push-policy"): {"policy": "build", "cap_micro_usd": 1_000_000},
        ("POST", "/api/v1/projects/{project_id}/uploads"): {},  # membership is checked before the archive
        ("PUT", "/api/v1/workspaces/{workspace_id}/provider-keys/{provider}"): {
            "api_key": "sk-ant-" + "x" * 40
        },
        ("PUT", "/api/v1/projects/{project_id}/access"): {"mode": "workspace"},
        ("PUT", "/api/v1/projects/{project_id}/access/members/{user_id}"): {"role": "editor"},
        ("PATCH", "/api/v1/workspaces/{workspace_id}"): {"name": "Hijacked"},
        ("POST", "/api/v1/workspaces/{workspace_id}/deletion"): {"confirmation": workspace_slug},
        ("POST", "/api/v1/workspaces/{workspace_id}/github/install"): {},
        ("POST", "/api/v1/workspaces/{workspace_id}/invitations"): {
            "email": "attacker@example.test",
            "role": "owner",
        },
        ("PATCH", "/api/v1/workspaces/{workspace_id}/members/{user_id}"): {"role": "viewer"},
        ("POST", "/api/v1/workspaces/{workspace_id}/projects"): {
            "repository_id": ids["repository_id"],
            "name": "Mine",
            "slug": "mine",
        },
        ("POST", "/api/v1/workspaces/{workspace_id}/support-access"): {"hours": 72, "reason": "let me in"},
        ("POST", "/api/v1/workspaces/{workspace_id}/access-tokens"): {
            "name": "stolen",
            "expires_in_days": 365,
        },
        ("POST", "/api/v1/workspaces/{workspace_id}/billing/checkout"): {"plan": "team"},
        ("POST", "/api/v1/workspaces/{workspace_id}/billing/portal"): {},
        ("POST", "/api/v1/workspaces/{workspace_id}/billing/trial"): {},
        ("POST", "/api/v1/ops/projects/{project_id}/suspension"): {"reason": "takedown"},
        ("POST", "/api/v1/ops/workspaces/{workspace_id}/entitlements"): {
            "allowance_micro_usd": 1_000_000_000,
            "days": 366,
            "reason": "free money",
        },
    }


def routes() -> list[tuple[str, str]]:
    return [(m.upper(), p) for p, ops in SPEC["paths"].items() for m in ops if (m.upper(), p) not in UNSCOPED]


def fill(path: str, ids: dict[str, str]) -> str:
    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        assert name in ids, f"unclassified path parameter {name} in {path}: add it to the sweep"
        return ids[name]

    return re.sub(r"{(\w+)}", value, path)


QUERY = {"q": "main", "path": "", "reason": "sweep reason", "connection_id": "", "action": ""}


async def call(
    person: httpx.AsyncClient, method: str, path: str, ids: dict[str, str], body: Any
) -> httpx.Response:
    url = fill(path, ids)
    params = {"q": "main"} if url.endswith("/search") else {}
    if "suspension" in path or "/entitlements/" in path:
        params["reason"] = "sweep reason"
    if path.endswith("/repositories"):
        params["connection_id"] = ids["connection_id"]
    headers = {"Idempotency-Key": secrets.token_hex(12), "If-Match": '"publication-1"'}
    if method == "GET":
        return await person.get(url, params=params)
    return await unsafe(person, method, url, json=body, params=params, headers=headers)


async def test_another_tenant_is_refused_on_every_route_and_changes_nothing(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    world = await victim(client, idp, github)
    ids, owner = world["ids"], world["owner"]
    stranger, _ws, _project = await setup_project(client, idp, github)
    payloads = bodies(ids, world["workspace_slug"])
    before = {
        "workspace": (await owner.get(f"/api/v1/workspaces/{ids['workspace_id']}")).json(),
        "members": (await owner.get(f"/api/v1/workspaces/{ids['workspace_id']}/members")).json(),
        "publication": (await owner.get(f"/api/v1/projects/{ids['project_id']}/publication")).json(),
        "builds": (await owner.get(f"/api/v1/projects/{ids['project_id']}/builds")).json(),
        "invitations": (await owner.get(f"/api/v1/workspaces/{ids['workspace_id']}/invitations")).json(),
        "audit": len(
            (
                await owner.get(f"/api/v1/workspaces/{ids['workspace_id']}/audit", params={"limit": 100})
            ).json()["items"]
        ),
    }
    secrets_of_victim = {
        ids[k] for k in ("workspace_id", "project_id", "build_id", "snapshot_id", "file_id", "symbol_id")
    }
    failures = []
    for method, path in routes():
        body = payloads.get((method, path)) if method != "GET" and method != "DELETE" else None
        if method in ("POST", "PATCH", "PUT") and body is None:
            failures.append(f"{method} {path}: no body in the sweep (classify the new route)")
            continue
        response = await call(stranger, method, path, ids, body)
        if response.status_code < 400 or response.status_code >= 500:
            failures.append(f"{method} {path}: {response.status_code}")
        elif method == "GET" and response.status_code != 404:
            failures.append(f"{method} {path}: {response.status_code} (not found expected)")
        leaked = [v for v in secrets_of_victim if v in response.text and v not in fill(path, ids)]
        if leaked:
            failures.append(f"{method} {path}: response names victim IDs")
    assert not failures, failures

    after = {
        "workspace": (await owner.get(f"/api/v1/workspaces/{ids['workspace_id']}")).json(),
        "members": (await owner.get(f"/api/v1/workspaces/{ids['workspace_id']}/members")).json(),
        "publication": (await owner.get(f"/api/v1/projects/{ids['project_id']}/publication")).json(),
        "builds": (await owner.get(f"/api/v1/projects/{ids['project_id']}/builds")).json(),
        "invitations": (await owner.get(f"/api/v1/workspaces/{ids['workspace_id']}/invitations")).json(),
        "audit": len(
            (
                await owner.get(f"/api/v1/workspaces/{ids['workspace_id']}/audit", params={"limit": 100})
            ).json()["items"]
        ),
    }
    assert after == before  # nothing changed, and the stranger left no trace in the victim's audit log
    assert (
        ddl("SELECT count(*) FROM deletion_requests WHERE workspace_id = :w", w=ids["workspace_id"])[0][0]
        == 0
    )
    assert ddl("SELECT revoked_at IS NULL FROM sessions WHERE id = :s", s=ids["session_id"])[0][0]
    assert ddl("SELECT revoked_at IS NULL FROM access_tokens WHERE id = :t", t=ids["token_id"])[0][0]
    assert ddl("SELECT revoked_at IS NULL FROM oauth_grants WHERE id = :g", g=ids["oauth_grant_id"])[0][0]
    assert ddl(
        "SELECT o.status, o.name, n.handle FROM organizations o JOIN namespaces n ON n.organization_id = o.id"
        " WHERE o.id = :o",
        o=ids["organization_id"],
    )[0][:2] == ("active", "Victim org")
    assert (
        ddl("SELECT count(*) FROM organization_members WHERE organization_id = :o", o=ids["organization_id"])[
            0
        ][0]
        == 1
    )
    assert ddl("SELECT decided_at IS NULL FROM oauth_authorizations WHERE id = :i", i=ids["request_id"])[0][0]


async def test_anonymous_visitors_get_nothing_from_private_routes(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    world = await victim(client, idp, github)
    ids = world["ids"]
    payloads = bodies(ids, world["workspace_slug"])
    failures = []
    async with browser(world["app"]) as visitor:
        for method, path in routes():
            url = fill(path, ids)
            if method == "GET":
                response = await visitor.get(url, params={"q": "main"} if url.endswith("/search") else {})
                expected = {404} if path.startswith("/api/v1/published/") else {401}  # private site: unknown
            else:
                response = await visitor.request(
                    method, url, json=payloads.get((method, path)), headers={"Origin": "http://evil.example"}
                )
                expected = {401, 403}
            if response.status_code not in expected:
                failures.append(f"{method} {path}: {response.status_code}")
    assert not failures, failures


async def test_tenant_content_is_only_ever_served_as_json_and_never_sniffed(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    world = await victim(client, idp, github)
    ids, owner = world["ids"], world["owner"]
    for path in (
        f"/api/v1/builds/{ids['build_id']}/files/{ids['file_id']}",
        f"/api/v1/builds/{ids['build_id']}/symbols/{ids['symbol_id']}",
        f"/api/v1/builds/{ids['build_id']}/guide/{ids['page']}",
        f"/api/v1/published/{ids['slug']}/files/{ids['file_id']}",
        f"/api/v1/published/{ids['slug']}/guide/{ids['page']}",
    ):
        response = await owner.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("application/json"), path
        assert response.headers["x-content-type-options"] == "nosniff", path
        assert response.headers["cache-control"] == "no-store", path
