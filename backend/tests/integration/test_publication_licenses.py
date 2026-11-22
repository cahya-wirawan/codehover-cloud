"""Public sites of public-source repositories need a license that permits redistribution (01 FR-029, 21).

The license is GitHub's answer at the build's exact commit; the license file must be in the site, and
the site names the license. Private sites, App-granted repositories and the database backstop are covered
too. GitHub is the fake from test_public_repos.
"""

from __future__ import annotations

import secrets
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from tests.integration.auth_support import ddl
from tests.integration.conftest import needs_db
from tests.integration.test_builds import drive
from tests.integration.test_generation import runtime_for
from tests.integration.test_github import FakeGitHub, github, workspace  # noqa: F401
from tests.integration.test_public_repos import FakePublicGitHub, build, create, public  # noqa: F401
from tests.integration.test_publication import publish
from tests.integration.test_workspaces import NewUser, code, new_user  # noqa: F401

pytestmark = [pytest.mark.integration, needs_db]
MIT = "MIT License\n\nCopyright (c) 2026 Octocat\n\nPermission is hereby granted, free of charge...\n"


async def ready_public_project(
    client: httpx.AsyncClient, new_user: NewUser, public: FakePublicGitHub, **repo: Any
) -> tuple[httpx.AsyncClient, str, str, int]:
    """(owner, project ID, ready build ID, GitHub repository ID) of a public-source project."""
    app = client.app  # type: ignore[attr-defined]
    repo_id = public.add("octocat", "hello", **repo)
    owner = await new_user()
    ws = await workspace(owner)
    project = (await create(owner, ws, "octocat/hello", slug=f"hello-{secrets.token_hex(3)}")).json()
    started = await build(owner, project["id"])
    assert started.status_code == 202, started.text
    assert await drive(app, started.json()["id"], runtime=runtime_for(app)) == ["completed"] * 4
    return owner, project["id"], started.json()["id"], repo_id


async def go_public(
    owner: httpx.AsyncClient, project: str, build_id: str, if_match: str | None = None
) -> httpx.Response:
    return await publish(
        owner,
        project,
        if_match,
        build_id=build_id,
        visibility="public",
        public_slug=f"site-{secrets.token_hex(4)}",
        confirmation="Hello",
    )


async def test_a_permitted_license_at_the_commit_allows_a_public_site_that_names_it(
    client: httpx.AsyncClient, new_user: NewUser, public: FakePublicGitHub
) -> None:
    public.extra_files = {"LICENSE": MIT}
    owner, project, build_id, _ = await ready_public_project(
        client, new_user, public, license=("MIT", "LICENSE")
    )
    published = await go_public(owner, project, build_id)
    assert published.status_code == 200, published.text
    assert published.json()["license_spdx"] == "MIT"
    assert public.license_refs == [public.commit_sha]  # asked about the build's exact commit

    site = (await client.get(f"/api/v1/published/{published.json()['address']}")).json()
    assert site["license"]["spdx_id"] == "MIT" and site["license"]["path"] == "LICENSE"
    shown = await client.get(
        f"/api/v1/published/{published.json()['address']}/files/{site['license']['file_id']}"
    )
    assert shown.status_code == 200 and "Permission is hereby granted" in shown.text  # the notice travels

    audit = ddl(
        "SELECT metadata->>'license' FROM audit_events WHERE action = 'publication.changed'"
        " AND resource_id = CAST(:p AS uuid)",
        p=project,
    )
    assert [row[0] for row in audit] == ["MIT"]
    await owner.aclose()


@pytest.mark.parametrize(
    ("license", "reason", "reported"),
    [
        (None, "no_license", None),  # all rights reserved
        (("NOASSERTION", "LICENSE"), "unrecognized", None),
        (("BUSL-1.1", "LICENSE"), "not_permitted", "BUSL-1.1"),
        (("MIT", "COPYING"), "license_file_missing", "MIT"),  # GitHub names a file the build lacks
    ],
)
async def test_other_licenses_allow_only_a_private_site(
    client: httpx.AsyncClient,
    new_user: NewUser,
    public: FakePublicGitHub,
    license: tuple[str, str] | None,
    reason: str,
    reported: str | None,
) -> None:
    public.extra_files = {"LICENSE": "All rights reserved.\n"}
    owner, project, build_id, _ = await ready_public_project(client, new_user, public, license=license)
    refused = await go_public(owner, project, build_id)
    assert refused.status_code == 409 and code(refused) == "LICENSE_NOT_PERMITTED", refused.text
    assert refused.json()["error"]["details"] == {"reason": reason, "license": reported}
    assert ddl("SELECT count(*) FROM publications WHERE project_id = CAST(:p AS uuid)", p=project)[0][0] == 0

    lookups = len(public.license_refs)
    private = await publish(owner, project, build_id=build_id)  # members only: not redistribution
    assert private.status_code == 200, private.text
    assert private.json()["license_spdx"] is None and len(public.license_refs) == lookups
    await owner.aclose()


async def test_a_repository_that_turned_private_cannot_go_public(
    client: httpx.AsyncClient, new_user: NewUser, public: FakePublicGitHub
) -> None:
    public.extra_files = {"LICENSE": MIT}
    owner, project, build_id, repo_id = await ready_public_project(
        client, new_user, public, license=("MIT", "LICENSE")
    )
    public.repos[repo_id].update(private=True, visibility="private")
    refused = await go_public(owner, project, build_id)
    assert refused.status_code == 409 and code(refused) == "REPOSITORY_NOT_PUBLIC"
    assert public.license_refs == []  # visibility is checked before the license
    await owner.aclose()


async def test_the_database_refuses_an_unchecked_public_site_of_a_public_source_repository(
    client: httpx.AsyncClient, new_user: NewUser, public: FakePublicGitHub
) -> None:
    public.extra_files = {"LICENSE": MIT}
    owner, project, build_id, _ = await ready_public_project(
        client, new_user, public, license=("MIT", "LICENSE")
    )
    private = await publish(owner, project, build_id=build_id)
    assert private.status_code == 200, private.text
    with pytest.raises(sa.exc.IntegrityError, match="needs a permitted license"):
        ddl(
            "UPDATE publications SET visibility = 'public' WHERE project_id = CAST(:p AS uuid)",
            p=project,
        )
    published = await go_public(owner, project, build_id, private.headers["etag"])  # with the check
    assert published.status_code == 200, published.text
    with pytest.raises(sa.exc.IntegrityError, match="needs a permitted license"):
        ddl(
            "UPDATE publications SET license_spdx = NULL, license_path = NULL WHERE project_id = CAST(:p AS uuid)",
            p=project,
        )
    await owner.aclose()


@pytest.mark.parametrize("reason", ["billing", "operator"])
async def test_lifting_another_suspension_never_brings_back_an_unchecked_public_site(
    client: httpx.AsyncClient, new_user: NewUser, public: FakePublicGitHub, reason: str
) -> None:
    """A public site that was already suspended (billing, takedown) when 0029 arrived missed its backfill;
    when billing recovers or the operator reinstates it, it stays offline as `license`."""
    public.extra_files = {"LICENSE": MIT}
    owner, project, build_id, _ = await ready_public_project(
        client, new_user, public, license=("MIT", "LICENSE")
    )
    private = await publish(owner, project, build_id=build_id)
    assert private.status_code == 200, private.text
    slug = private.json()["address"]
    ddl(  # the state of such a site before 0029: public, unlicensed, suspended for another reason
        "UPDATE publications SET visibility = 'public', suspended_at = now(),"
        " suspended_reason = :r WHERE project_id = CAST(:p AS uuid)",
        r=reason,
        p=project,
    )
    ddl(  # the billing sync's or the operator's lift
        "UPDATE publications SET suspended_at = NULL, suspended_reason = NULL"
        " WHERE project_id = CAST(:p AS uuid) AND suspended_reason = :r",
        r=reason,
        p=project,
    )
    state = ddl("SELECT suspended_reason FROM publications WHERE project_id = CAST(:p AS uuid)", p=project)
    assert state[0][0] == "license"
    assert (await client.get(f"/api/v1/published/{slug}")).status_code == 404

    etag = (await owner.get(f"/api/v1/projects/{project}/publication")).headers["etag"]
    republished = await go_public(owner, project, build_id, etag)  # the admin publishes, with the check
    assert republished.status_code == 200, republished.text
    assert republished.json()["suspended_reason"] is None and republished.json()["license_spdx"] == "MIT"
    await owner.aclose()
