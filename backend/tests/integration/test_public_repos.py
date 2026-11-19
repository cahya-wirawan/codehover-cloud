"""Public GitHub repositories without the App (01 "Public repository source", 08 "Public repository reads").

A fake of GitHub's public endpoints records every request, so the tests can assert that source downloads
are anonymous even when the operator token is configured, and that a repository is checked to be public
whenever it is resolved or downloaded.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from app.adapters.github import app as gha
from app.adapters.github.public import PublicGitHub
from tests.integration.auth_support import FakeIdP, ddl, unsafe
from tests.integration.conftest import needs_db
from tests.integration.test_builds import drive, fixture_tarball
from tests.integration.test_generation import runtime_for
from tests.integration.test_github import FakeGitHub, connect, github, github_user, workspace  # noqa: F401
from tests.integration.test_workspaces import NewUser, code, new_user  # noqa: F401

pytestmark = [pytest.mark.integration, needs_db]
TOKEN = "github_pat_public_only"


@dataclass
class FakePublicGitHub:
    repos: dict[int, dict[str, Any]] = field(default_factory=dict)
    commit_sha: str = "feed" + "0" * 36
    requests: list[tuple[str, str]] = field(default_factory=list)  # (url, authorization header)

    def add(self, owner: str, name: str, *, private: bool = False, repo_id: int | None = None) -> int:
        repo_id = repo_id or secrets.randbelow(10**9) + 1
        self.repos[repo_id] = {
            "id": repo_id,
            "name": name,
            "owner": {"login": owner},
            "private": private,
            "visibility": "private" if private else "public",
            "default_branch": "trunk",
        }
        return repo_id

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        self.requests.append((url, request.headers.get("authorization", "")))
        if url.startswith("https://codeload.github.com/"):
            owner, name, _, sha = url.removeprefix("https://codeload.github.com/").split("/")
            repo = next(
                (r for r in self.repos.values() if (r["owner"]["login"], r["name"]) == (owner, name)), None
            )
            if repo is None or repo["private"] or sha != self.commit_sha:
                return httpx.Response(404)  # codeload serves nothing private to anonymous readers
            return httpx.Response(200, content=fixture_tarball())
        for repo in self.repos.values():
            base = f"{gha.API}/repositories/{repo['id']}"
            visible = not repo["private"] or request.headers.get("authorization") == f"Bearer {TOKEN}"
            if url in (f"{gha.API}/repos/{repo['owner']['login']}/{repo['name']}", base) and visible:
                return httpx.Response(
                    200, json=repo
                )  # a token that can see private repositories still answers
            if url.startswith(f"{base}/commits/") and visible:
                ref = url.rsplit("/", 1)[-1]
                return (
                    httpx.Response(200, json={"sha": self.commit_sha})
                    if ref in ("trunk", self.commit_sha)
                    else httpx.Response(422)
                )
            if url == f"{base}/branches" and visible:
                return httpx.Response(200, json=[{"name": "trunk", "commit": {"sha": self.commit_sha}}])
            if url == f"{base}/tags" and visible:
                return httpx.Response(200, json=[])
        return httpx.Response(404)


@pytest.fixture
def public(client: httpx.AsyncClient) -> FakePublicGitHub:
    fake = FakePublicGitHub()
    client.app.state.public_github = PublicGitHub(  # type: ignore[attr-defined]
        httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)), TOKEN
    )
    return fake


async def create(owner: httpx.AsyncClient, ws: str, repository: str, slug: str = "hello") -> httpx.Response:
    return await unsafe(
        owner,
        "POST",
        f"/api/v1/workspaces/{ws}/projects",
        json={"source": "public", "public_repository": repository, "name": "Hello", "slug": slug},
    )


async def build(owner: httpx.AsyncClient, project: str, **body: Any) -> httpx.Response:
    return await unsafe(
        owner,
        "POST",
        f"/api/v1/projects/{project}/builds",
        json=body,
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )


async def test_a_public_repository_builds_without_the_app_and_downloads_anonymously(
    client: httpx.AsyncClient, new_user: NewUser, public: FakePublicGitHub
) -> None:
    app = client.app  # type: ignore[attr-defined]
    assert app.state.github_app is None  # no GitHub App configured at all
    repo_id = public.add("octocat", "hello")
    owner = await new_user()
    ws = await workspace(owner)
    created = await create(owner, ws, "https://github.com/octocat/hello.git")
    assert created.status_code == 201, created.text
    project = created.json()
    assert (project["source"], project["owner"], project["repository"], project["tracked_ref"]) == (
        "public",
        "octocat",
        "hello",
        "trunk",  # the repository's default branch
    )
    assert (await owner.get(f"/api/v1/workspaces/{ws}/connections")).json()[
        "items"
    ] == []  # not an App connection

    started = await build(owner, project["id"])
    assert started.status_code == 202, started.text
    outcomes = await drive(app, started.json()["id"], runtime=runtime_for(app))
    assert outcomes == ["completed"] * 4, outcomes
    status = (await owner.get(f"/api/v1/builds/{started.json()['id']}")).json()
    assert status["status"] == "ready" and status["commit_sha"] == public.commit_sha

    downloads = [(url, auth) for url, auth in public.requests if "codeload" in url]
    assert downloads == [(f"https://codeload.github.com/octocat/hello/legacy.tar.gz/{public.commit_sha}", "")]
    assert all(auth == f"Bearer {TOKEN}" for url, auth in public.requests if "api.github.com" in url)
    assert any(url == f"{gha.API}/repositories/{repo_id}" for url, _ in public.requests)  # re-checked by ID

    freshness = (await owner.get(f"/api/v1/projects/{project['id']}/freshness")).json()
    assert freshness["state"] == "current" and freshness["latest_sha"] == public.commit_sha
    repository = ddl("SELECT repository_id FROM projects WHERE id = :p", p=project["id"])[0][0]
    refs = await owner.get(f"/api/v1/workspaces/{ws}/repositories/{repository}/refs")
    assert refs.status_code == 200 and refs.json()["items"] == [
        {"name": "trunk", "kind": "branch", "sha": public.commit_sha}
    ]
    # A second project of the same repository shares its row; there are no webhooks without the App.
    second = await create(owner, ws, "octocat/hello", slug="hello-2")
    assert ddl("SELECT repository_id FROM projects WHERE id = :p", p=second.json()["id"])[0][0] == repository
    etag = (await owner.get(f"/api/v1/projects/{project['id']}/push-policy")).headers["etag"]
    pushes = await unsafe(
        owner,
        "PUT",
        f"/api/v1/projects/{project['id']}/push-policy",
        json={"policy": "snapshot"},
        headers={"If-Match": etag},
    )
    assert pushes.status_code == 409 and code(pushes) == "NOT_A_GITHUB_PROJECT"
    await owner.aclose()


async def test_private_missing_and_malformed_repositories_are_refused(
    client: httpx.AsyncClient, new_user: NewUser, public: FakePublicGitHub
) -> None:
    public.add("octocat", "secret", private=True)  # the operator token can see it; it is still refused
    owner = await new_user()
    ws = await workspace(owner)
    for repository in ("octocat/secret", "octocat/missing"):
        refused = await create(owner, ws, repository)
        assert refused.status_code == 404 and code(refused) == "REPOSITORY_NOT_PUBLIC", repository
    for repository in ("https://evil.example/octocat/hello", "octocat", "octocat/hello/tree/main", "../x/y"):
        refused = await create(owner, ws, repository)
        assert refused.status_code == 422 and code(refused) == "REPOSITORY_INVALID", repository
    assert not any("evil" in url for url, _ in public.requests)  # nothing a user types becomes a host
    await owner.aclose()


async def test_a_repository_that_turns_private_stops_building(
    client: httpx.AsyncClient, new_user: NewUser, public: FakePublicGitHub
) -> None:
    app = client.app  # type: ignore[attr-defined]
    repo_id = public.add("octocat", "hello")
    owner = await new_user()
    ws = await workspace(owner)
    project = (await create(owner, ws, "octocat/hello")).json()
    queued = await build(owner, project["id"])
    assert queued.status_code == 202
    public.repos[repo_id]["private"] = True  # between the request and the download
    public.repos[repo_id]["visibility"] = "private"
    assert await drive(app, queued.json()["id"], runtime=runtime_for(app)) == ["failed"]
    stages = (await owner.get(f"/api/v1/builds/{queued.json()['id']}")).json()["stages"]
    assert stages[0]["error_code"] == "repository_not_public"
    assert not any("codeload" in url for url, _ in public.requests)  # nothing was downloaded

    refused = await build(owner, project["id"], ref="trunk")
    assert refused.status_code == 404 and code(refused) == "REPOSITORY_NOT_PUBLIC"
    public.repos[repo_id]["private"] = False
    public.repos[repo_id]["visibility"] = "public"
    missing = await build(owner, project["id"], ref="no-such-branch")
    assert missing.status_code == 422 and code(missing) == "REF_NOT_FOUND"
    await owner.aclose()


async def test_the_app_and_the_public_source_share_one_repository_row(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, public: FakePublicGitHub
) -> None:
    owner, gh_user = await github_user(client, idp)
    ws = await workspace(owner)
    installation = github.add_installation(gh_user)
    web = next(r for r in github.repos[installation] if r["name"] == "web")
    web.update(private=False, visibility="public")
    public.add("octo-org", "web", repo_id=web["id"])
    public.add("octocat", "hello")
    first = (await create(owner, ws, "octocat/hello")).json()
    assert first["source"] == "public"

    await connect(owner, github, ws, installation, gh_user)  # the App grants octo-org/web
    reused = await create(owner, ws, "octo-org/web", slug="web")
    assert reused.status_code == 201 and reused.json()["source"] == "github"  # read through the App
    rows = ddl(
        "SELECT count(*) FROM repositories WHERE workspace_id = :w AND provider_repo_id = :r",
        w=ws,
        r=web["id"],
    )
    assert rows[0][0] == 1

    # An admin revokes the App connection: the public source must not quietly lift that.
    connection = (await owner.get(f"/api/v1/workspaces/{ws}/connections")).json()["items"][0]["id"]
    revoked = await unsafe(owner, "DELETE", f"/api/v1/workspaces/{ws}/connections/{connection}")
    assert revoked.status_code == 204
    refused = await create(owner, ws, "octo-org/web", slug="web-again")
    assert refused.status_code == 409 and code(refused) == "CONNECTION_INACTIVE"
    state = ddl(
        "SELECT c.provider, c.status FROM repositories r JOIN repository_connections c ON c.id = r.connection_id"
        " WHERE r.workspace_id = :w AND r.provider_repo_id = :r",
        w=ws,
        r=web["id"],
    )
    assert tuple(state[0]) == ("github", "revoked")  # the row stays with the revoked App connection
    await owner.aclose()
