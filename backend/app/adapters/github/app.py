"""GitHub App client (09 "Repository authorization", 22).

Two kinds of tokens, never persisted and never logged:
- a user access token *for this App*, obtained once per installation to prove that the person
  connecting can access the installation (`GET /user/installations`), then discarded;
- short-lived installation tokens (App JWT -> `POST /app/installations/{id}/access_tokens`), cached in
  memory until shortly before expiry, used for repository metadata and refs.
Endpoints are fixed; repositories are addressed by numeric ID so renames cannot redirect access.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from joserfc import jwt
from joserfc.jwk import RSAKey

WEB = "https://github.com"
API = "https://api.github.com"
API_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
MAX_PAGES = 10
PER_PAGE = 100
TOKEN_REFRESH_MARGIN_SECONDS = 120


class GitHubError(Exception):
    """GitHub call failed. `reason` is safe to log; `status` is GitHub's HTTP status (0 = network)."""

    def __init__(self, reason: str, status: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class Installation:
    id: int
    account_id: int
    account_login: str


@dataclass(frozen=True)
class RepositoryInfo:
    id: int
    owner: str
    name: str
    visibility: str
    default_branch: str


@dataclass(frozen=True)
class Ref:
    name: str
    kind: str  # "branch" | "tag"
    sha: str


class GitHubApp:
    def __init__(
        self,
        *,
        app_id: str,
        slug: str,
        client_id: str,
        client_secret: str,
        private_key_pem: str,
        http: httpx.AsyncClient,
    ) -> None:
        self.slug = slug
        self._app_id = app_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._key = RSAKey.import_key(private_key_pem)
        self._http = http
        self._installation_tokens: dict[int, tuple[str, float]] = {}

    # ---------------------------------------------------------------- browser redirects
    def installation_url(self, state: str) -> str:
        return f"{WEB}/apps/{self.slug}/installations/new?{urlencode({'state': state})}"

    def user_authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{WEB}/login/oauth/authorize?{urlencode(params)}"

    # ---------------------------------------------------------------- user token (verification only)
    async def exchange_user_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> str:
        response = await self._http.post(
            f"{WEB}/login/oauth/access_token",
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
            },
            headers={"Accept": "application/json"},
        )
        body = response.json() if response.status_code == 200 else {}
        token = body.get("access_token")
        if not isinstance(token, str) or body.get("error"):
            raise GitHubError("user_token_exchange_failed", response.status_code)
        return token

    async def user_id(self, user_token: str) -> int:
        body = await self._get_json(f"{API}/user", user_token)
        if not isinstance(body, dict) or not isinstance(body.get("id"), int):
            raise GitHubError("user_profile_invalid")
        user_id: int = body["id"]
        return user_id

    async def user_installations(self, user_token: str) -> list[Installation]:
        found: list[Installation] = []
        for page in range(1, MAX_PAGES + 1):
            body = await self._get_json(f"{API}/user/installations", user_token, page=page)
            items = body.get("installations", []) if isinstance(body, dict) else []
            for item in items:
                account = item.get("account") or {}
                if isinstance(item.get("id"), int) and isinstance(account.get("id"), int):
                    found.append(Installation(item["id"], account["id"], str(account.get("login", ""))))
            if len(items) < PER_PAGE:
                break
        return found

    # ---------------------------------------------------------------- installation token
    def _app_jwt(self) -> str:
        now = int(time.time())
        claims = {"iat": now - 60, "exp": now + 540, "iss": self._app_id}
        return jwt.encode({"alg": "RS256"}, claims, self._key)

    async def installation_token(self, installation_id: int) -> str:
        cached = self._installation_tokens.get(installation_id)
        if cached and cached[1] - TOKEN_REFRESH_MARGIN_SECONDS > time.time():
            return cached[0]
        response = await self._http.post(
            f"{API}/app/installations/{installation_id}/access_tokens",
            headers={**API_HEADERS, "Authorization": f"Bearer {self._app_jwt()}"},
        )
        if response.status_code != 201:
            raise GitHubError("installation_token_failed", response.status_code)
        body = response.json()
        expires = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00")).timestamp()
        self._installation_tokens[installation_id] = (body["token"], expires)
        token: str = body["token"]
        return token

    def forget_installation(self, installation_id: int) -> None:
        self._installation_tokens.pop(installation_id, None)

    # ---------------------------------------------------------------- repository data
    async def installation_repositories(self, installation_id: int) -> list[RepositoryInfo]:
        token = await self.installation_token(installation_id)
        repos: list[RepositoryInfo] = []
        for page in range(1, MAX_PAGES + 1):
            body = await self._get_json(f"{API}/installation/repositories", token, page=page)
            items = body.get("repositories", []) if isinstance(body, dict) else []
            repos += [_repository(item) for item in items]
            if len(items) < PER_PAGE:
                break
        return repos

    async def repository(self, installation_id: int, repo_id: int) -> RepositoryInfo:
        token = await self.installation_token(installation_id)
        return _repository(await self._get_json(f"{API}/repositories/{repo_id}", token))

    async def refs(self, installation_id: int, repo_id: int) -> list[Ref]:
        token = await self.installation_token(installation_id)
        refs: list[Ref] = []
        for kind, path in (("branch", "branches"), ("tag", "tags")):
            body = await self._get_json(f"{API}/repositories/{repo_id}/{path}", token, page=1)
            for item in body if isinstance(body, list) else []:
                sha = (item.get("commit") or {}).get("sha")
                if isinstance(item.get("name"), str) and isinstance(sha, str):
                    refs.append(Ref(item["name"], kind, sha))
        return refs

    # ---------------------------------------------------------------- transport
    async def _get_json(self, url: str, token: str, *, page: int | None = None) -> Any:
        params = {"per_page": PER_PAGE, "page": page} if page else None
        try:
            response = await self._http.get(
                url, params=params, headers={**API_HEADERS, "Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            raise GitHubError("network") from exc
        if response.status_code != 200:
            raise GitHubError("api_error", response.status_code)
        return response.json()


def _repository(item: dict[str, Any]) -> RepositoryInfo:
    owner = item.get("owner") or {}
    visibility = item.get("visibility") or ("private" if item.get("private") else "public")
    return RepositoryInfo(
        id=int(item["id"]),
        owner=str(owner.get("login", "")),
        name=str(item.get("name", "")),
        visibility=visibility if visibility in ("public", "private", "internal") else "private",
        default_branch=str(item.get("default_branch") or "main"),
    )
