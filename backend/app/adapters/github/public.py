"""Public GitHub repositories without the GitHub App (01 "Public repository source", 08).

Metadata comes from api.github.com: anonymously, or with the operator's optional `GITHUB_PUBLIC_TOKEN`,
which only raises the rate limit (60 to 5,000 calls an hour, shared by every workspace on the server).
Every answer is checked to be a public repository, so a token that can see private repositories never
lets one through. The source itself is always downloaded anonymously from codeload.github.com, where
only public content is readable at all. Repositories are addressed by numeric ID after the first lookup,
so a rename never redirects access; hosts are fixed and nothing a user types becomes a URL (08 SSRF).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx

from app.adapters.github.app import API, API_HEADERS, GitHubError, Ref, RepositoryInfo, _repository

CODELOAD = "https://codeload.github.com"
# GitHub's own limits: owners up to 39 characters, repository names up to 100.
OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def parse_repository(value: str) -> tuple[str, str]:
    """`owner/name`, `github.com/owner/name` or a github.com URL (optionally ending in `.git`). Anything
    else, including other hosts, is refused: the server only ever talks to GitHub's fixed hosts."""
    text = value.strip()
    for prefix in ("https://github.com/", "http://github.com/", "https://www.github.com/", "github.com/"):
        if text.lower().startswith(prefix):
            text = text[len(prefix) :]
            break
    parts = text.strip("/").split("/")
    if len(parts) != 2:
        raise ValueError("expected owner/name")
    owner, name = parts[0], parts[1].removesuffix(".git")
    if not OWNER.fullmatch(owner) or not NAME.fullmatch(name) or name in (".", ".."):
        raise ValueError("expected owner/name")
    return owner, name


class PublicGitHub:
    def __init__(self, http: httpx.AsyncClient, token: str | None = None) -> None:
        self._http = http
        self._token = token

    async def lookup(self, owner: str, name: str) -> RepositoryInfo:
        """The public repository `owner/name`. A private or missing one is `not_found` either way."""
        return self._public(await self._get(f"{API}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"))

    async def repository(self, repo_id: int) -> RepositoryInfo:
        """The repository by numeric ID, refused unless it is still public."""
        return self._public(await self._get(f"{API}/repositories/{repo_id}"))

    async def refs(self, repo_id: int) -> list[Ref]:
        await self.repository(repo_id)
        refs: list[Ref] = []
        for kind, path in (("branch", "branches"), ("tag", "tags")):
            body = await self._get(f"{API}/repositories/{repo_id}/{path}", params={"per_page": 100})
            for item in body if isinstance(body, list) else []:
                sha = (item.get("commit") or {}).get("sha")
                if isinstance(item.get("name"), str) and isinstance(sha, str):
                    refs.append(Ref(item["name"], kind, sha))
        return refs

    async def resolve_ref(self, repo_id: int, ref: str) -> str:
        """Exact commit SHA for a branch, tag or SHA of a repository that is still public (01 FR-004)."""
        await self.repository(repo_id)
        body = await self._get(f"{API}/repositories/{repo_id}/commits/{quote(ref, safe='')}")
        sha = body.get("sha") if isinstance(body, dict) else None
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise GitHubError("ref_not_found", 404)
        return sha

    async def download_tarball(self, repo_id: int, sha: str, max_bytes: int) -> bytes:
        """The commit's tarball, anonymously from codeload: re-checks that the repository is public, then
        uses its current owner and name. The body is capped while streaming."""
        if not SHA.fullmatch(sha):
            raise GitHubError("ref_not_found", 404)
        repo = await self.repository(repo_id)
        url = f"{CODELOAD}/{quote(repo.owner, safe='')}/{quote(repo.name, safe='')}/legacy.tar.gz/{sha}"
        chunks: list[bytes] = []
        total = 0
        try:
            async with self._http.stream("GET", url, headers={"Accept": "application/gzip"}) as download:
                if download.status_code != 200:
                    raise GitHubError("tarball_unavailable", download.status_code)
                async for chunk in download.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise GitHubError("tarball_too_large")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise GitHubError("network") from exc
        return b"".join(chunks)

    # ---------------------------------------------------------------- transport
    @staticmethod
    def _public(body: Any) -> RepositoryInfo:
        if (
            not isinstance(body, dict)
            or body.get("private") is not False
            or body.get("visibility", "public") != "public"
        ):
            raise GitHubError("not_found", 404)  # private, internal or unknown: indistinguishable to callers
        return _repository(body)

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        headers = dict(API_HEADERS)
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            response = await self._http.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise GitHubError("network") from exc
        if response.status_code == 404:
            raise GitHubError("not_found", 404)
        if response.status_code in (403, 429) and response.headers.get("x-ratelimit-remaining") == "0":
            raise GitHubError("rate_limited", 429)
        if response.status_code != 200:
            raise GitHubError("api_error", response.status_code)
        return response.json()
