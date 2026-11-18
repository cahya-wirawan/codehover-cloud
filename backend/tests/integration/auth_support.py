"""Shared sign-in machinery for integration tests: fake Google/GitHub endpoints and an app factory.

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
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
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
from tests.integration.conftest import MIGRATION_URL, RUNTIME_URL, engine

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


def issues_session(response: httpx.Response) -> bool:
    """True if any Set-Cookie header sets (not clears) the session cookie."""
    return any(
        header.startswith("__Host-codehover_session=") and "max-age=0" not in header.lower()
        for header in response.headers.get_list("set-cookie")
    )


def ddl(sql: str, **params: Any) -> list[Any]:
    with engine(MIGRATION_URL).begin() as conn:
        result = conn.execute(sa.text(sql), params)
        return list(result) if result.returns_rows else []


def make_app(idp: FakeIdP) -> tuple[Any, httpx.AsyncClient]:
    """The real application wired to the fake providers. Returns (app, provider HTTP client)."""
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
    return app, mock_http


def browser(app: Any) -> httpx.AsyncClient:
    """A separate browser (own cookie jar) against the same application."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN)
