"""Google OpenID Connect, authorization code + PKCE (09 "Sign-in and session design").

Endpoints are fixed (no runtime discovery) so configuration cannot redirect token traffic. The ID
token is verified locally: RS256 signature against Google's JWKS, issuer, audience, expiry and nonce.
"""

from __future__ import annotations

import time
from urllib.parse import urlencode

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from app.adapters.oauth import AuthorizationRequest, OAuthError, ProviderName, VerifiedIdentity

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint, not a secret
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
JWKS_TTL_SECONDS = 3600
CLOCK_SKEW_SECONDS = 60


class GoogleProvider:
    name: ProviderName = "google"
    uses_nonce = True

    def __init__(self, client_id: str, client_secret: str, http: httpx.AsyncClient) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._jwks: KeySet | None = None
        self._jwks_expires = 0.0

    def authorization_url(self, request: AuthorizationRequest) -> str:
        params = {
            "client_id": self._client_id,
            "redirect_uri": request.redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": request.state,
            "nonce": request.nonce or "",
            "code_challenge": request.code_challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    async def _keys(self, *, refresh: bool = False) -> KeySet:
        if refresh or self._jwks is None or time.monotonic() > self._jwks_expires:
            response = await self._http.get(JWKS_URL)
            if response.status_code != 200:
                raise OAuthError("jwks_unavailable")
            self._jwks = KeySet.import_key_set(response.json())
            self._jwks_expires = time.monotonic() + JWKS_TTL_SECONDS
        return self._jwks

    async def exchange(
        self, *, code: str, code_verifier: str, redirect_uri: str, nonce: str | None
    ) -> VerifiedIdentity:
        if not nonce:
            raise OAuthError("nonce_missing")
        response = await self._http.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            headers={"Accept": "application/json"},
        )
        if response.status_code != 200:
            raise OAuthError("token_exchange_failed")
        id_token = response.json().get("id_token")
        if not isinstance(id_token, str):
            raise OAuthError("id_token_missing")
        claims = await self._verify(id_token, nonce)
        raw_email, raw_name = claims.get("email"), claims.get("name")
        email = (
            raw_email.lower() if isinstance(raw_email, str) and claims.get("email_verified") is True else None
        )
        name = raw_name if isinstance(raw_name, str) and raw_name else None
        return VerifiedIdentity(
            provider="google",
            issuer="https://accounts.google.com",
            subject=str(claims["sub"]),
            display_name=(name or email or "Google user")[:120],
            verified_email=email,
            # No hint: an email address or a name never becomes a public handle (a random one is suggested).
        )

    async def _verify(self, id_token: str, nonce: str) -> dict[str, object]:
        registry = jwt.JWTClaimsRegistry(
            leeway=CLOCK_SKEW_SECONDS,
            iss={"essential": True, "values": list(ISSUERS)},
            aud={"essential": True, "value": self._client_id},
            sub={"essential": True},
            exp={"essential": True},
            iat={"essential": True},
            nonce={"essential": True, "value": nonce},
        )
        for refresh in (False, True):  # retry once with fresh keys: Google rotates its signing keys
            try:
                token = jwt.decode(id_token, await self._keys(refresh=refresh), algorithms=["RS256"])
            except JoseError as exc:
                if refresh:
                    raise OAuthError("id_token_invalid") from exc
                continue
            try:
                registry.validate(token.claims)
            except JoseError as exc:
                raise OAuthError("id_token_claims_invalid") from exc
            return dict(token.claims)
        raise OAuthError("id_token_invalid")
