"""Sign-in, sessions, CSRF and linking against PostgreSQL 18 with mocked identity providers (T03).

The fake providers serve real artifacts: RS256-signed ID tokens from a locally generated key, and a
token endpoint that checks the PKCE verifier against the challenge from the authorization URL. So the
application's verification code runs for real; only the network is replaced.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import sqlalchemy as sa
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey
from pydantic import SecretStr

from app.adapters.oauth import github as gh
from app.adapters.oauth import google as gg
from app.adapters.oauth.github import GitHubProvider
from app.adapters.oauth.google import GoogleProvider
from app.main import create_app
from tests.conftest import make_settings
from tests.integration.conftest import MIGRATION_URL, RUNTIME_URL, engine, needs_db

pytestmark = [pytest.mark.integration, needs_db]

ORIGIN = "https://app.example.test"
CLIENT_ID = "test-client.apps.example"
SIGNING_KEY = RSAKey.generate_key(2048, parameters={"kid": "test-1", "alg": "RS256", "use": "sig"})
OTHER_KEY = RSAKey.generate_key(2048, parameters={"kid": "test-1", "alg": "RS256", "use": "sig"})


@dataclass
class FakeIdP:
    """Google and GitHub endpoints. Tests set the identity the next exchange should return."""

    google_sub: str = field(default_factory=lambda: secrets.token_hex(8))
    google_email: str | None = None
    google_claims: dict[str, Any] = field(default_factory=dict)
    google_key: RSAKey = field(default_factory=lambda: SIGNING_KEY)
    forge: str | None = None  # "none" or "hs256": algorithm-confusion attacks
    github_id: int = field(default_factory=lambda: secrets.randbelow(10**9) + 1)
    github_emails: list[dict[str, Any]] = field(default_factory=list)
    challenges: dict[str, str] = field(default_factory=dict)  # state -> code_challenge
    nonces: dict[str, str] = field(default_factory=dict)  # state -> nonce
    codes: dict[str, str] = field(default_factory=dict)  # code -> state

    def authorize(self, location: str) -> str:
        """Simulate the user approving at the provider: returns the callback query `code`."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
        self.challenges[query["state"]] = query["code_challenge"]
        if "nonce" in query:
            self.nonces[query["state"]] = query["nonce"]
        code = secrets.token_urlsafe(16)
        self.codes[code] = query["state"]
        return code

    def _pkce_ok(self, form: dict[str, str]) -> bool:
        state = self.codes.get(form.get("code", ""))
        if state is None:
            return False
        digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == self.challenges[state]

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        if url == gg.JWKS_URL:
            return httpx.Response(
                200, json=KeySet([RSAKey.import_key(self.google_key.as_dict(private=False))]).as_dict()
            )
        if url in (gg.TOKEN_URL, gh.TOKEN_URL):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if not self._pkce_ok(form):
                return httpx.Response(400, json={"error": "invalid_grant"})
            if url == gh.TOKEN_URL:
                return httpx.Response(200, json={"access_token": "gho_test", "token_type": "bearer"})
            now = int(time.time())
            claims = {
                "iss": "https://accounts.google.com",
                "aud": CLIENT_ID,
                "sub": self.google_sub,
                "iat": now,
                "exp": now + 300,
                "nonce": self.nonces[self.codes[form["code"]]],
                "name": "Grace Hopper",
                "email": self.google_email,
                "email_verified": self.google_email is not None,
                **self.google_claims,
            }
            token = (
                forged(self.forge, claims)
                if self.forge
                else jwt.encode({"alg": "RS256", "kid": "test-1"}, claims, SIGNING_KEY)
            )
            return httpx.Response(200, json={"id_token": token, "access_token": "ya29.test"})
        if url == f"{gh.API}/user":
            return httpx.Response(200, json={"id": self.github_id, "login": "octocat", "name": "Octo Cat"})
        if url == f"{gh.API}/user/emails":
            return httpx.Response(200, json=self.github_emails)
        return httpx.Response(404)


def forged(kind: str, claims: dict[str, Any]) -> str:
    """Tokens a verifier must reject even though their claims are perfect."""

    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    if kind == "none":
        return f"{part({'alg': 'none', 'kid': 'test-1'})}.{part(claims)}."
    # HS256 keyed with the RSA public key: the classic RS/HS confusion attack. Built by hand, as an
    # attacker would; joserfc itself refuses to use a public key as an HMAC secret.
    signing_input = f"{part({'alg': 'HS256', 'kid': 'test-1'})}.{part(claims)}"
    mac = hmac.new(SIGNING_KEY.as_pem(private=False), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"


@pytest.fixture
def idp() -> FakeIdP:
    return FakeIdP()


@pytest.fixture
async def client(idp: FakeIdP, migrated: None) -> AsyncIterator[httpx.AsyncClient]:
    assert RUNTIME_URL
    settings = make_settings(
        database_url=SecretStr(RUNTIME_URL),
        public_origin=ORIGIN,
        session_cookie_secure=True,
        google_client_id=CLIENT_ID,
        google_client_secret=SecretStr("g-secret"),
        github_oauth_client_id="gh-client",
        github_oauth_client_secret=SecretStr("gh-secret"),
    )
    app = create_app(settings)
    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(idp.handler))
    app.state.oauth_providers = {
        "google": GoogleProvider(CLIENT_ID, "g-secret", mock_http),
        "github": GitHubProvider("gh-client", "gh-secret", mock_http),
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
        yield c
    await mock_http.aclose()
    await app.state.engine.dispose()


async def sign_in(
    client: httpx.AsyncClient, idp: FakeIdP, provider: str = "google", return_to: str = "/"
) -> httpx.Response:
    start = await client.get(f"/api/v1/auth/{provider}/start", params={"return_to": return_to})
    assert start.status_code == 302
    location = start.headers["location"]
    code = idp.authorize(location)
    state = parse_qs(urlsplit(location).query)["state"][0]
    return await client.get(f"/api/v1/auth/{provider}/callback", params={"code": code, "state": state})


async def unsafe(client: httpx.AsyncClient, method: str, url: str, **kw: Any) -> httpx.Response:
    token = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    headers = {"Origin": ORIGIN, "X-CSRF-Token": token, **kw.pop("headers", {})}
    return await client.request(method, url, headers=headers, **kw)


def ddl(sql: str, **params: Any) -> list[Any]:
    with engine(MIGRATION_URL).begin() as conn:
        result = conn.execute(sa.text(sql), params)
        return list(result) if result.returns_rows else []


# ---------------------------------------------------------------- sign-in
async def test_google_sign_in_sets_host_cookie_and_returns_to_safe_path(
    client: httpx.AsyncClient, idp: FakeIdP
) -> None:
    idp.google_email = "Grace@Example.Test"
    response = await sign_in(client, idp, return_to="/w/settings?tab=members")
    assert response.status_code == 302 and response.headers["location"] == "/w/settings?tab=members"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-codehover_session=")
    for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"):
        assert attribute.lower() in cookie.lower()
    assert "domain" not in cookie.lower()
    me = (await client.get("/api/v1/me")).json()
    assert me["display_name"] == "Grace Hopper" and me["primary_email"] == "grace@example.test"
    assert [i["provider"] for i in me["identities"]] == ["google"]
    assert (
        ddl(
            "SELECT count(*) FROM audit_events WHERE actor_user_id = :u AND action = 'session.created'",
            u=me["id"],
        )[0][0]
        == 1
    )


async def test_unverified_email_is_not_trusted(client: httpx.AsyncClient, idp: FakeIdP) -> None:
    idp.github_emails = [{"email": "unverified@example.test", "primary": True, "verified": False}]
    await sign_in(client, idp, provider="github")
    me = (await client.get("/api/v1/me")).json()
    assert me["primary_email"] is None and me["identities"][0]["verified_email"] is None


async def test_same_email_on_two_providers_is_never_merged(client: httpx.AsyncClient, idp: FakeIdP) -> None:
    shared = f"shared-{secrets.token_hex(4)}@example.test"
    idp.google_email = shared
    idp.github_emails = [{"email": shared, "primary": True, "verified": True}]
    await sign_in(client, idp, provider="google")
    google_user = (await client.get("/api/v1/me")).json()["id"]
    client.cookies.clear()
    await sign_in(client, idp, provider="github")
    assert (await client.get("/api/v1/me")).json()["id"] != google_user


async def test_returning_user_gets_same_account_and_old_session_is_rotated(
    client: httpx.AsyncClient, idp: FakeIdP
) -> None:
    await sign_in(client, idp)
    first = (await client.get("/api/v1/me/sessions")).json()["items"]
    await sign_in(client, idp)  # signing in again while signed in rotates the session
    sessions = (await client.get("/api/v1/me/sessions")).json()["items"]
    assert len(first) == 1 and len(sessions) == 1 and sessions[0]["id"] != first[0]["id"]


@pytest.mark.parametrize(
    "unsafe_return", ["/\t/evil.example", "//evil.example", "https://evil.example", "/a/../b"]
)
async def test_unsafe_return_path_falls_back_to_root(
    client: httpx.AsyncClient, idp: FakeIdP, unsafe_return: str
) -> None:
    response = await sign_in(client, idp, return_to=unsafe_return)
    assert response.headers["location"] == "/"


# ---------------------------------------------------------------- callback attacks
async def test_state_is_single_use(client: httpx.AsyncClient, idp: FakeIdP) -> None:
    start = await client.get("/api/v1/auth/google/start")
    code = idp.authorize(start.headers["location"])
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    first = await client.get("/api/v1/auth/google/callback", params={"code": code, "state": state})
    client.cookies.clear()
    replay = await client.get("/api/v1/auth/google/callback", params={"code": code, "state": state})
    assert first.headers["location"] == "/"
    assert replay.headers["location"] == "/login?error=expired" and "set-cookie" not in replay.headers


async def test_state_from_another_provider_is_rejected(client: httpx.AsyncClient, idp: FakeIdP) -> None:
    start = await client.get("/api/v1/auth/google/start")
    code = idp.authorize(start.headers["location"])
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    response = await client.get("/api/v1/auth/github/callback", params={"code": code, "state": state})
    assert response.headers["location"] == "/login?error=expired"


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(lambda idp: idp.google_claims.update(aud="someone-else"), id="audience"),
        pytest.param(lambda idp: idp.google_claims.update(iss="https://evil.example"), id="issuer"),
        pytest.param(lambda idp: idp.google_claims.update(exp=int(time.time()) - 600), id="expired"),
        pytest.param(lambda idp: idp.google_claims.update(nonce="replayed-nonce"), id="nonce"),
        pytest.param(lambda idp: setattr(idp, "google_key", OTHER_KEY), id="signature"),
        pytest.param(lambda idp: setattr(idp, "forge", "none"), id="alg-none"),
        pytest.param(lambda idp: setattr(idp, "forge", "hs256"), id="alg-confusion-hs256"),
    ],
)
async def test_invalid_id_tokens_are_rejected(
    client: httpx.AsyncClient, idp: FakeIdP, tamper: Callable[[FakeIdP], None]
) -> None:
    tamper(idp)
    response = await sign_in(client, idp)
    assert response.headers["location"] == "/login?error=provider" and "set-cookie" not in response.headers


async def test_provider_denial_and_missing_provider(client: httpx.AsyncClient) -> None:
    denied = await client.get("/api/v1/auth/google/callback", params={"error": "access_denied", "state": "x"})
    assert denied.headers["location"] == "/login?error=denied"
    del client._transport.app.state.oauth_providers["github"]  # type: ignore[attr-defined]
    unavailable = await client.get("/api/v1/auth/github/start")
    assert unavailable.headers["location"] == "/login?error=unavailable"


# ---------------------------------------------------------------- sessions and CSRF
async def test_logout_requires_csrf_token_and_same_origin(client: httpx.AsyncClient, idp: FakeIdP) -> None:
    await sign_in(client, idp)
    token = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    no_token = await client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN})
    bad_origin = await client.post(
        "/api/v1/auth/logout", headers={"Origin": "https://evil.example", "X-CSRF-Token": token}
    )
    wrong_token = await client.post(
        "/api/v1/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": "x" * 43}
    )
    assert [r.json()["error"]["code"] for r in (no_token, bad_origin, wrong_token)] == [
        "CSRF_REJECTED",
        "ORIGIN_REJECTED",
        "CSRF_REJECTED",
    ]
    ok = await client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": token})
    assert ok.status_code == 204 and "max-age=0" in ok.headers["set-cookie"].lower()
    assert (await client.get("/api/v1/me")).status_code == 401


async def test_revoked_or_idle_sessions_are_not_accepted(client: httpx.AsyncClient, idp: FakeIdP) -> None:
    await sign_in(client, idp)
    user = (await client.get("/api/v1/me")).json()["id"]
    ddl("UPDATE sessions SET last_seen_at = now() - interval '8 days' WHERE user_id = :u", u=user)
    response = await client.get("/api/v1/me")
    assert response.status_code == 401 and response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert response.headers["cache-control"] == "no-store"


async def test_user_can_revoke_another_own_session_but_not_someone_elses(
    client: httpx.AsyncClient, idp: FakeIdP
) -> None:
    await sign_in(client, idp)
    first_cookies = dict(client.cookies)
    client.cookies.clear()
    await sign_in(client, idp)  # second device, same account
    sessions = (await client.get("/api/v1/me/sessions")).json()["items"]
    other = next(s for s in sessions if not s["current"])
    assert (await unsafe(client, "DELETE", f"/api/v1/me/sessions/{other['id']}")).status_code == 204
    client.cookies.clear()
    client.cookies.update(first_cookies)
    assert (await client.get("/api/v1/me")).status_code == 401  # the revoked device is signed out
    client.cookies.clear()
    idp.google_sub = secrets.token_hex(8)  # a different person
    await sign_in(client, idp)
    assert (await unsafe(client, "DELETE", f"/api/v1/me/sessions/{other['id']}")).status_code == 404


# ---------------------------------------------------------------- linked identities
async def start_link(client: httpx.AsyncClient, provider: str) -> httpx.Response:
    return await unsafe(client, "POST", f"/api/v1/me/identities/{provider}/link")


async def test_link_requires_recent_sign_in(client: httpx.AsyncClient, idp: FakeIdP) -> None:
    await sign_in(client, idp)
    user = (await client.get("/api/v1/me")).json()["id"]
    ddl("UPDATE sessions SET reauthenticated_at = now() - interval '11 minutes' WHERE user_id = :u", u=user)
    response = await start_link(client, "github")
    assert response.status_code == 403 and response.json()["error"]["code"] == "REAUTH_REQUIRED"


async def test_link_then_unlink_and_last_identity_is_protected(
    client: httpx.AsyncClient, idp: FakeIdP
) -> None:
    await sign_in(client, idp)
    link = (await start_link(client, "github")).json()["redirect_url"]
    code = idp.authorize(link)
    state = parse_qs(urlsplit(link).query)["state"][0]
    done = await client.get("/api/v1/auth/github/callback", params={"code": code, "state": state})
    assert done.headers["location"] == "/account/security"
    identities = (await client.get("/api/v1/me")).json()["identities"]
    assert sorted(i["provider"] for i in identities) == ["github", "google"]
    github = next(i for i in identities if i["provider"] == "github")
    assert (await unsafe(client, "DELETE", f"/api/v1/me/identities/{github['id']}")).status_code == 204
    google = (await client.get("/api/v1/me")).json()["identities"][0]
    last = await unsafe(client, "DELETE", f"/api/v1/me/identities/{google['id']}")
    assert last.status_code == 409 and last.json()["error"]["code"] == "LAST_IDENTITY"


async def test_linking_someone_elses_identity_is_a_conflict_not_a_merge(
    client: httpx.AsyncClient, idp: FakeIdP
) -> None:
    await sign_in(client, idp, provider="github")  # this GitHub account belongs to user B
    owner_b = (await client.get("/api/v1/me")).json()["id"]
    client.cookies.clear()
    idp.google_sub = secrets.token_hex(8)
    await sign_in(client, idp, provider="google")  # user A
    link = (await start_link(client, "github")).json()["redirect_url"]
    code = idp.authorize(link)
    state = parse_qs(urlsplit(link).query)["state"][0]
    response = await client.get("/api/v1/auth/github/callback", params={"code": code, "state": state})
    assert response.headers["location"] == "/account/security?error=link_conflict"
    me = (await client.get("/api/v1/me")).json()
    assert me["id"] != owner_b and [i["provider"] for i in me["identities"]] == ["google"]


async def test_link_callback_from_another_session_is_rejected(
    client: httpx.AsyncClient, idp: FakeIdP
) -> None:
    await sign_in(client, idp)
    link = (await start_link(client, "github")).json()["redirect_url"]
    code = idp.authorize(link)
    state = parse_qs(urlsplit(link).query)["state"][0]
    client.cookies.clear()  # an attacker's browser completing the victim's link
    response = await client.get("/api/v1/auth/github/callback", params={"code": code, "state": state})
    assert response.headers["location"] == "/login?error=expired"


async def test_another_signed_in_user_cannot_complete_someone_elses_link(
    client: httpx.AsyncClient, idp: FakeIdP
) -> None:
    await sign_in(client, idp)  # user A starts linking GitHub
    link = (await start_link(client, "github")).json()["redirect_url"]
    code = idp.authorize(link)
    state = parse_qs(urlsplit(link).query)["state"][0]
    client.cookies.clear()
    idp.google_sub = secrets.token_hex(8)
    await sign_in(client, idp)  # user B, signed in, completes A's callback
    response = await client.get("/api/v1/auth/github/callback", params={"code": code, "state": state})
    assert response.headers["location"] == "/login?error=expired"
    assert [i["provider"] for i in (await client.get("/api/v1/me")).json()["identities"]] == ["google"]
