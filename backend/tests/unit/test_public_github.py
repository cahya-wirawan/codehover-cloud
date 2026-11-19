"""Public GitHub repositories (01, 08): strict names, and only public answers accepted."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.adapters.github.app import GitHubError
from app.adapters.github.public import PublicGitHub, parse_repository


def test_repository_names_are_parsed_strictly() -> None:
    assert parse_repository("octocat/Hello-World") == ("octocat", "Hello-World")
    assert parse_repository(" https://github.com/octocat/hello.git/ ") == ("octocat", "hello")
    assert parse_repository("github.com/a-b/c.d_e") == ("a-b", "c.d_e")
    for bad in ("octocat", "a/b/c", "-bad/x", "a/..", "https://gitlab.com/a/b", "a/b c", "x" * 40 + "/y"):
        with pytest.raises(ValueError):
            parse_repository(bad)


def test_answers_about_private_repositories_are_refused_even_with_a_token() -> None:
    """The operator token may see private repositories; the client must never accept one."""
    answers = {
        "private": {"id": 1, "name": "x", "owner": {"login": "o"}, "private": True, "visibility": "private"},
        "internal": {
            "id": 2,
            "name": "x",
            "owner": {"login": "o"},
            "private": False,
            "visibility": "internal",
        },
        "unknown": {"id": 3, "name": "x", "owner": {"login": "o"}},
    }
    for body in answers.values():
        client = PublicGitHub(
            httpx.AsyncClient(transport=httpx.MockTransport(lambda _r, b=body: httpx.Response(200, json=b))),
            "t",
        )
        with pytest.raises(GitHubError, match="not_found"):
            asyncio.run(client.lookup("o", "x"))


def test_rate_limits_are_reported_as_such() -> None:
    limited = httpx.Response(403, headers={"x-ratelimit-remaining": "0"})
    client = PublicGitHub(httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: limited)))
    with pytest.raises(GitHubError, match="rate_limited"):
        asyncio.run(client.repository(1))


def _licensed(answer: httpx.Response, *, public: bool = True) -> tuple[PublicGitHub, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    repo = {"id": 7, "name": "x", "owner": {"login": "o"}, "private": not public}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer if request.url.path.endswith("/license") else httpx.Response(200, json=repo)

    return PublicGitHub(httpx.AsyncClient(transport=httpx.MockTransport(handler))), seen


def test_the_license_is_asked_for_at_the_exact_commit() -> None:
    sha = "ab" * 20
    found = httpx.Response(200, json={"path": "LICENSE-MIT", "license": {"spdx_id": "MIT"}})
    client, seen = _licensed(found)
    license = asyncio.run(client.license(7, sha))
    assert license is not None and (license.spdx_id, license.path) == ("MIT", "LICENSE-MIT")
    assert [r.url.path for r in seen] == ["/repositories/7", "/repositories/7/license"]
    assert seen[1].url.params["ref"] == sha


def test_missing_unnamed_and_private_licenses() -> None:
    sha = "ab" * 20
    assert asyncio.run(_licensed(httpx.Response(404))[0].license(7, sha)) is None  # no license file
    unnamed = httpx.Response(200, json={"path": "COPYING", "license": {"spdx_id": None}})
    license = asyncio.run(_licensed(unnamed)[0].license(7, sha))
    assert license is not None and license.spdx_id == "NOASSERTION"
    client, seen = _licensed(httpx.Response(200, json={"path": "LICENSE"}), public=False)
    with pytest.raises(GitHubError, match="not_found"):  # visibility first: a private repository is refused
        asyncio.run(client.license(7, sha))
    assert [r.url.path for r in seen] == ["/repositories/7"]
    with pytest.raises(GitHubError, match="ref_not_found"):
        asyncio.run(_licensed(httpx.Response(404))[0].license(7, "main"))  # only commit SHAs
