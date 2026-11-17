"""GitHub App connection, repositories and refs against PostgreSQL 18 with a fake GitHub (T05; A03).

The fake verifies what real GitHub would: PKCE on the App's user-token exchange, the App JWT signature
and issuer before minting installation tokens, and repository access per installation. Sign-in and the
App share the token endpoint, so the fake routes by client_id, as GitHub does.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import RSAKey

from app.adapters.github import app as gha
from app.adapters.github.app import GitHubApp
from tests.integration.auth_support import FakeIdP, browser, ddl, sign_in, unsafe
from tests.integration.conftest import needs_db

pytestmark = [pytest.mark.integration, needs_db]

APP_ID = "424242"
APP_CLIENT_ID = "Iv1.app-client"
APP_KEY = RSAKey.generate_key(2048)


@dataclass
class FakeGitHub:
    idp: FakeIdP
    installations: dict[int, list[dict[str, Any]]] = field(
        default_factory=dict
    )  # github user -> installations
    repos: dict[int, list[dict[str, Any]]] = field(default_factory=dict)  # installation -> repositories
    challenges: dict[str, str] = field(default_factory=dict)
    codes: dict[str, tuple[str, int]] = field(default_factory=dict)  # code -> (state, github user)
    user_access: dict[tuple[int, int], set[str]] = field(default_factory=dict)  # (user, inst) -> repo names

    def share(self, installation_id: int, github_user: int, repo_names: set[str]) -> None:
        """Give another GitHub user access to some repositories of an existing installation."""
        entry = next(i for items in self.installations.values() for i in items if i["id"] == installation_id)
        self.installations.setdefault(github_user, []).append(entry)
        self.user_access[(github_user, installation_id)] = repo_names

    def add_installation(self, owner_github_id: int, login: str = "octo-org") -> int:
        installation_id = secrets.randbelow(10**9) + 1
        account = {"id": secrets.randbelow(10**9) + 1, "login": login}
        self.installations.setdefault(owner_github_id, []).append({"id": installation_id, "account": account})
        self.repos[installation_id] = [
            {
                "id": secrets.randbelow(10**9) + 1,
                "name": name,
                "owner": {"login": login},
                "private": True,
                "visibility": "private",
                "default_branch": "main",
            }
            for name in ("api", "web", "secret")
        ]
        return installation_id

    def authorize(self, location: str, as_github_user: int) -> str:
        query = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
        assert query["client_id"] == APP_CLIENT_ID
        self.challenges[query["state"]] = query["code_challenge"]
        code = secrets.token_urlsafe(12)
        self.codes[code] = (query["state"], as_github_user)
        return code

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        auth = request.headers.get("authorization", "")
        if url == f"{gha.WEB}/login/oauth/access_token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form.get("client_id") != APP_CLIENT_ID:
                return self.idp.handler(request)  # sign-in OAuth app
            state, user = self.codes.pop(form.get("code", ""), ("", 0))
            verifier_hash = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
            if (
                not state
                or base64.urlsafe_b64encode(verifier_hash).rstrip(b"=").decode() != self.challenges[state]
            ):
                return httpx.Response(200, json={"error": "bad_verification_code"})
            return httpx.Response(200, json={"access_token": f"ghu_{user}"})
        if auth.startswith("Bearer ghu_"):
            user = int(auth.removeprefix("Bearer ghu_"))
            if url == f"{gha.API}/user":
                return httpx.Response(200, json={"id": user, "login": f"user{user}"})
            if url == f"{gha.API}/user/installations":
                items = self.installations.get(user, [])
                return httpx.Response(200, json={"total_count": len(items), "installations": items})
            if url.startswith(f"{gha.API}/user/installations/") and url.endswith("/repositories"):
                installation = int(url.split("/")[-2])
                if not any(i["id"] == installation for i in self.installations.get(user, [])):
                    return httpx.Response(404)
                allowed = self.user_access.get((user, installation))
                repos = [r for r in self.repos[installation] if allowed is None or r["name"] in allowed]
                return httpx.Response(200, json={"total_count": len(repos), "repositories": repos})
        if url.startswith(f"{gha.API}/app/installations/") and url.endswith("/access_tokens"):
            try:
                token = jwt.decode(auth.removeprefix("Bearer "), APP_KEY, algorithms=["RS256"])
            except JoseError:
                return httpx.Response(401)
            if token.claims.get("iss") != APP_ID:
                return httpx.Response(401)
            installation = int(url.split("/")[-2])
            if installation not in self.repos:
                return httpx.Response(404)
            expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
            return httpx.Response(201, json={"token": f"ghs_{installation}", "expires_at": expires})
        if auth.startswith("Bearer ghs_"):
            installation = int(auth.removeprefix("Bearer ghs_"))
            repos = self.repos.get(installation, [])
            if url == f"{gha.API}/installation/repositories":
                return httpx.Response(200, json={"total_count": len(repos), "repositories": repos})
            for repo in repos:
                base = f"{gha.API}/repositories/{repo['id']}"
                if url == f"{base}/branches":
                    return httpx.Response(200, json=[{"name": "main", "commit": {"sha": "a" * 40}}])
                if url == f"{base}/tags":
                    return httpx.Response(200, json=[{"name": "v1.0.0", "commit": {"sha": "b" * 40}}])
            return httpx.Response(404)
        return self.idp.handler(request)


@pytest.fixture
def github(client: httpx.AsyncClient, idp: FakeIdP) -> FakeGitHub:
    fake = FakeGitHub(idp)
    app = client.app  # type: ignore[attr-defined]
    app.state.github_app = GitHubApp(
        app_id=APP_ID,
        slug="codehover-test",
        client_id=APP_CLIENT_ID,
        client_secret="app-secret",
        private_key_pem=APP_KEY.as_pem(private=True).decode(),
        http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    return fake


async def github_user(client: httpx.AsyncClient, idp: FakeIdP) -> tuple[httpx.AsyncClient, int]:
    """A person who signed in with GitHub (so their GitHub identity is linked), in a fresh browser."""
    person = browser(client.app)  # type: ignore[attr-defined]
    idp.github_id = secrets.randbelow(10**9) + 1
    await sign_in(person, idp, provider="github")
    return person, idp.github_id


async def workspace(owner: httpx.AsyncClient) -> str:
    response = await unsafe(
        owner, "POST", "/api/v1/workspaces", json={"name": "Team", "slug": f"g-{secrets.token_hex(4)}"}
    )
    ws: str = response.json()["id"]
    return ws


async def connect(
    person: httpx.AsyncClient, github: FakeGitHub, ws: str, installation_id: int, as_github_user: int
) -> str:
    """Run the whole connect flow; returns the final redirect location."""
    start = await unsafe(person, "POST", f"/api/v1/workspaces/{ws}/github/install")
    assert start.status_code == 200, start.text
    state = parse_qs(urlsplit(start.json()["install_url"]).query)["state"][0]
    setup = await person.get(
        "/api/v1/integrations/github/setup", params={"installation_id": installation_id, "state": state}
    )
    location = setup.headers["location"]
    if not location.startswith("https://github.com/login/oauth/authorize"):
        return location
    code = github.authorize(location, as_github_user)
    next_state = parse_qs(urlsplit(location).query)["state"][0]
    done = await person.get(
        "/api/v1/integrations/github/callback", params={"code": code, "state": next_state}
    )
    location_final: str = done.headers["location"]
    return location_final


def connections(ws: str) -> list[Any]:
    return ddl("SELECT installation_id, status FROM repository_connections WHERE workspace_id = :w", w=ws)


# ---------------------------------------------------------------- connect flow
async def test_connect_then_list_repositories_and_refs(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, gh_user = await github_user(client, idp)
    ws = await workspace(owner)
    installation = github.add_installation(gh_user)
    assert await connect(owner, github, ws, installation, gh_user) == f"/w/{ws}/connections?connected=1"
    listed = (await owner.get(f"/api/v1/workspaces/{ws}/connections")).json()["items"]
    assert [(c["installation_id"], c["status"], c["account"]) for c in listed] == [
        (installation, "active", "octo-org")
    ]
    repos = (
        await owner.get(f"/api/v1/workspaces/{ws}/repositories", params={"connection_id": listed[0]["id"]})
    ).json()
    assert sorted(r["name"] for r in repos["items"]) == ["api", "secret", "web"]
    refs = (await owner.get(f"/api/v1/workspaces/{ws}/repositories/{repos['items'][0]['id']}/refs")).json()[
        "items"
    ]
    assert {(r["name"], r["kind"]) for r in refs} == {("main", "branch"), ("v1.0.0", "tag")}
    audit = ddl(
        "SELECT action FROM audit_events WHERE workspace_id = :w AND action = 'connection.created'", w=ws
    )
    assert len(audit) == 1
    await owner.aclose()


async def test_spoofed_or_someone_elses_installation_is_refused(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, gh_user = await github_user(client, idp)
    ws = await workspace(owner)
    github.add_installation(gh_user)
    someone_else = github.add_installation(owner_github_id=999_999_999, login="victim-org")
    for installation_id in (someone_else, 123_456_789):  # another user's installation, then a made-up ID
        result = await connect(owner, github, ws, installation_id, gh_user)
        assert result == f"/w/{ws}/connections?error=installation_not_accessible"
    assert connections(ws) == []
    await owner.aclose()


async def test_authorizing_as_a_different_github_account_is_refused(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _gh_user = await github_user(client, idp)
    ws = await workspace(owner)
    other_account = 555_000_111
    installation = github.add_installation(other_account)  # the other account really can access it
    result = await connect(owner, github, ws, installation, as_github_user=other_account)
    assert result == f"/w/{ws}/connections?error=identity_mismatch" and connections(ws) == []
    await owner.aclose()


async def test_google_only_user_must_link_github_first(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    await sign_in(client, idp, provider="google")
    ws = await workspace(client)
    installation = github.add_installation(777)
    result = await connect(client, github, ws, installation, as_github_user=777)
    assert result == f"/w/{ws}/connections?error=github_identity_required" and connections(ws) == []


async def test_states_are_browser_bound_and_phase_specific(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, gh_user = await github_user(client, idp)
    ws = await workspace(owner)
    installation = github.add_installation(gh_user)
    start = await unsafe(owner, "POST", f"/api/v1/workspaces/{ws}/github/install")
    state = parse_qs(urlsplit(start.json()["install_url"]).query)["state"][0]
    # The install-phase state used at the callback (phase confusion) is refused...
    confused = await owner.get("/api/v1/integrations/github/callback", params={"code": "x", "state": state})
    assert "error=expired" in confused.headers["location"]
    # ...and a fresh install state from another browser (no flow cookie) cannot continue the setup.
    start = await unsafe(owner, "POST", f"/api/v1/workspaces/{ws}/github/install")
    state = parse_qs(urlsplit(start.json()["install_url"]).query)["state"][0]
    other_browser = browser(client.app)  # type: ignore[attr-defined]
    hijack = await other_browser.get(
        "/api/v1/integrations/github/setup", params={"installation_id": installation, "state": state}
    )
    assert "error=expired" in hijack.headers["location"] and connections(ws) == []
    await other_browser.aclose()
    await owner.aclose()


# ---------------------------------------------------------------- roles, scope, lifecycle
async def test_roles_and_cross_workspace_access(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, gh_user = await github_user(client, idp)
    ws = await workspace(owner)
    installation = github.add_installation(gh_user)
    await connect(owner, github, ws, installation, gh_user)
    connection = (await owner.get(f"/api/v1/workspaces/{ws}/connections")).json()["items"][0]["id"]
    repo = (
        await owner.get(f"/api/v1/workspaces/{ws}/repositories", params={"connection_id": connection})
    ).json()["items"][0]

    viewer, _ = await github_user(client, idp)
    idp.google_email = None
    invite = await unsafe(
        owner,
        "POST",
        f"/api/v1/workspaces/{ws}/invitations",
        json={"email": "viewer@example.test", "role": "viewer"},
    )
    ddl(
        "UPDATE oauth_identities SET verified_email = 'viewer@example.test' WHERE user_id = :u",
        u=(await viewer.get("/api/v1/me")).json()["id"],
    )
    token = parse_qs(urlsplit(invite.json()["accept_url"]).fragment)["token"][0]
    assert (
        await unsafe(viewer, "POST", "/api/v1/invitations/accept", json={"token": token})
    ).status_code == 200
    assert (await unsafe(viewer, "POST", f"/api/v1/workspaces/{ws}/github/install")).status_code == 403
    assert (
        await viewer.get(f"/api/v1/workspaces/{ws}/repositories", params={"connection_id": connection})
    ).status_code == 403

    stranger, _ = await github_user(client, idp)
    other_ws = await workspace(stranger)
    for url in (
        f"/api/v1/workspaces/{ws}/repositories/{repo['id']}/refs",
        f"/api/v1/workspaces/{other_ws}/repositories/{repo['id']}/refs",
    ):
        assert (await stranger.get(url)).status_code == 404  # not a member / not this workspace's repository
    for person in (viewer, stranger, owner):
        await person.aclose()


async def test_repository_removed_from_installation_and_revoked_connection(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, gh_user = await github_user(client, idp)
    ws = await workspace(owner)
    installation = github.add_installation(gh_user)
    await connect(owner, github, ws, installation, gh_user)
    connection = (await owner.get(f"/api/v1/workspaces/{ws}/connections")).json()["items"][0]["id"]
    repos = (
        await owner.get(f"/api/v1/workspaces/{ws}/repositories", params={"connection_id": connection})
    ).json()["items"]
    github.repos[installation] = github.repos[installation][
        1:
    ]  # owner removed one repo from the installation
    gone = next(
        r for r in repos if r["provider_repo_id"] not in {x["id"] for x in github.repos[installation]}
    )
    assert (await owner.get(f"/api/v1/workspaces/{ws}/repositories/{gone['id']}/refs")).status_code == 404
    assert (
        await unsafe(owner, "DELETE", f"/api/v1/workspaces/{ws}/connections/{connection}")
    ).status_code == 204
    blocked = await owner.get(f"/api/v1/workspaces/{ws}/repositories", params={"connection_id": connection})
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "CONNECTION_INACTIVE"
    await owner.aclose()


async def test_install_without_configured_app(client: httpx.AsyncClient, idp: FakeIdP) -> None:
    await sign_in(client, idp)
    ws = await workspace(client)
    response = await unsafe(client, "POST", f"/api/v1/workspaces/{ws}/github/install")
    assert response.status_code == 503 and response.json()["error"]["code"] == "GITHUB_APP_UNAVAILABLE"


async def test_admin_demoted_mid_flow_cannot_finish(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ = await github_user(client, idp)
    admin, admin_gh = await github_user(client, idp)
    ws = await workspace(owner)
    admin_id = (await admin.get("/api/v1/me")).json()["id"]
    ddl("INSERT INTO memberships(workspace_id, user_id, role) VALUES (:w, :u, 'admin')", w=ws, u=admin_id)
    installation = github.add_installation(admin_gh)
    start = await unsafe(admin, "POST", f"/api/v1/workspaces/{ws}/github/install")
    state = parse_qs(urlsplit(start.json()["install_url"]).query)["state"][0]
    setup = await admin.get(
        "/api/v1/integrations/github/setup", params={"installation_id": installation, "state": state}
    )
    location = setup.headers["location"]
    demote = await unsafe(
        owner, "PATCH", f"/api/v1/workspaces/{ws}/members/{admin_id}", json={"role": "editor"}
    )
    assert demote.status_code == 204
    code = github.authorize(location, admin_gh)
    done = await admin.get(
        "/api/v1/integrations/github/callback",
        params={"code": code, "state": parse_qs(urlsplit(location).query)["state"][0]},
    )
    assert done.headers["location"] == f"/w/{ws}/connections?error=forbidden" and connections(ws) == []
    await owner.aclose()
    await admin.aclose()


async def test_flow_must_finish_in_the_session_that_started_it(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, gh_user = await github_user(client, idp)
    ws = await workspace(owner)
    installation = github.add_installation(gh_user)
    start = await unsafe(owner, "POST", f"/api/v1/workspaces/{ws}/github/install")
    state = parse_qs(urlsplit(start.json()["install_url"]).query)["state"][0]
    # Even holding the right flow cookie, another signed-in session cannot continue the flow.
    other, _ = await github_user(client, idp)
    other.cookies.set("__Host-codehover_oauth", owner.cookies["__Host-codehover_oauth"])
    hijack = await other.get(
        "/api/v1/integrations/github/setup", params={"installation_id": installation, "state": state}
    )
    assert "error=expired" in hijack.headers["location"] and connections(ws) == []
    await other.aclose()
    await owner.aclose()


# ---------------------------------------------------------------- repository grants (security review)
async def repo_names(person: httpx.AsyncClient, ws: str) -> dict[str, str]:
    connection = (await person.get(f"/api/v1/workspaces/{ws}/connections")).json()["items"][0]["id"]
    items = (
        await person.get(f"/api/v1/workspaces/{ws}/repositories", params={"connection_id": connection})
    ).json()
    return {r["name"]: r["id"] for r in items["items"]}


async def test_connection_grants_only_repositories_the_connector_can_read(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    # An org member who can read one repository sees the installation in /user/installations,
    # but must not gain the installation's other repositories.
    owner, _ = await github_user(client, idp)
    org_admin = 31_337
    installation = github.add_installation(org_admin)
    member_gh: int
    member, member_gh = await github_user(client, idp)
    github.share(installation, member_gh, {"api"})
    ws = await workspace(member)
    assert await connect(member, github, ws, installation, member_gh) == f"/w/{ws}/connections?connected=1"
    assert set(await repo_names(member, ws)) == {"api"}
    stored = ddl("SELECT name FROM repositories WHERE workspace_id = :w AND access_revoked_at IS NULL", w=ws)
    assert [r[0] for r in stored] == ["api"]
    await owner.aclose()
    await member.aclose()


async def test_reconnecting_with_narrower_access_revokes_the_rest(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, owner_gh = await github_user(client, idp)
    ws = await workspace(owner)
    installation = github.add_installation(owner_gh)
    await connect(owner, github, ws, installation, owner_gh)
    before = await repo_names(owner, ws)
    assert set(before) == {"api", "web", "secret"}
    admin, admin_gh = await github_user(client, idp)
    admin_id = (await admin.get("/api/v1/me")).json()["id"]
    ddl("INSERT INTO memberships(workspace_id, user_id, role) VALUES (:w, :u, 'admin')", w=ws, u=admin_id)
    github.share(installation, admin_gh, {"api"})
    assert await connect(admin, github, ws, installation, admin_gh) == f"/w/{ws}/connections?connected=1"
    assert set(await repo_names(owner, ws)) == {"api"}
    refs = await owner.get(f"/api/v1/workspaces/{ws}/repositories/{before['secret']}/refs")
    assert refs.status_code == 404  # revoked grant, even though the installation could still read it
    await owner.aclose()
    await admin.aclose()
