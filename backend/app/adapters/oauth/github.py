"""GitHub OAuth for identity only, authorization code + PKCE (09).

Scopes `read:user user:email`. The identity is the numeric user ID, not the mutable login or an email.
Only a primary, verified address from /user/emails is used. The access token is discarded after use.
"""

from __future__ import annotations

from urllib.parse import urlencode

import httpx

from app.adapters.oauth import AuthorizationRequest, OAuthError, ProviderName, VerifiedIdentity

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105 - endpoint, not a secret
API = "https://api.github.com"
ISSUER = "https://github.com"


class GitHubProvider:
    name: ProviderName = "github"
    uses_nonce = False

    def __init__(self, client_id: str, client_secret: str, http: httpx.AsyncClient) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http

    def authorization_url(self, request: AuthorizationRequest) -> str:
        params = {
            "client_id": self._client_id,
            "redirect_uri": request.redirect_uri,
            "scope": "read:user user:email",
            "state": request.state,
            "code_challenge": request.code_challenge,
            "code_challenge_method": "S256",
            "allow_signup": "true",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange(
        self, *, code: str, code_verifier: str, redirect_uri: str, nonce: str | None
    ) -> VerifiedIdentity:
        token_response = await self._http.post(
            TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
            },
            headers={"Accept": "application/json"},
        )
        body = token_response.json() if token_response.status_code == 200 else {}
        access_token = body.get("access_token")
        if not isinstance(access_token, str) or body.get("error"):
            raise OAuthError("token_exchange_failed")
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        user_response = await self._http.get(f"{API}/user", headers=headers)
        emails_response = await self._http.get(f"{API}/user/emails", headers=headers)
        if user_response.status_code != 200 or emails_response.status_code != 200:
            raise OAuthError("profile_unavailable")
        user = user_response.json()
        if not isinstance(user.get("id"), int):
            raise OAuthError("profile_invalid")
        email = next(
            (
                e["email"].lower()
                for e in emails_response.json()
                if isinstance(e, dict)
                and e.get("primary")
                and e.get("verified")
                and isinstance(e.get("email"), str)
            ),
            None,
        )
        name = (
            user.get("name") if isinstance(user.get("name"), str) and user.get("name") else user.get("login")
        )
        return VerifiedIdentity(
            provider="github",
            issuer=ISSUER,
            subject=str(user["id"]),
            display_name=str(name or "GitHub user")[:120],
            verified_email=email,
            handle_hint=str(user["login"])[:39] if isinstance(user.get("login"), str) else None,
        )
