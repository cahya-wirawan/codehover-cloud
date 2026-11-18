"""Build-scoped search (T14; 05 "Search documents", 06, A04, A07)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.integration.auth_support import FakeIdP
from tests.integration.conftest import needs_db
from tests.integration.test_builds import drive, setup_project
from tests.integration.test_generation import generation_build, runtime_for
from tests.integration.test_github import FakeGitHub, github  # noqa: F401

pytestmark = [pytest.mark.integration, needs_db]


async def find(owner: httpx.AsyncClient, build: str, q: str, **params: Any) -> list[dict[str, Any]]:
    response = await owner.get(f"/api/v1/builds/{build}/search", params={"q": q, **params})
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


async def test_search_ranks_names_then_text_within_one_build(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, build, app = await generation_build(client, idp, github, preset="replay-v2")
    await drive(app, build, runtime=runtime_for(app))

    exact = await find(owner, build, "helper")
    assert (
        exact[0]["name"] == "helper"
        and exact[0]["basis"] == "exact_name"
        and exact[0]["path"] == "src/util.py"
    )
    assert exact[0]["kind"] == "symbol" and exact[0]["line"] == 1
    prefix = await find(owner, build, "gree")
    assert {h["name"] for h in prefix} >= {"Greeter", "greet"} and all(
        h["basis"] == "name_prefix" for h in prefix[:2]
    )
    text = await find(owner, build, "entry point")
    assert text and text[0]["name"] == "main" and text[0]["basis"] == "text"  # from the docstring summary
    files = await find(owner, build, "util", kind="file")
    assert [h["path"] for h in files] == ["src/util.py"]
    guide = await find(owner, build, "how", kind="guide")
    assert guide and all(h["kind"] == "guide" for h in guide)
    assert await find(owner, build, "%_\\'") == []  # LIKE and tsquery syntax are inert
    assert await find(owner, build, "zzzz-nothing") == []
    too_long = await owner.get(f"/api/v1/builds/{build}/search", params={"q": "x" * 201})
    assert too_long.status_code == 422


async def test_excluded_content_is_never_searchable_and_other_tenants_see_nothing(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, build, app = await generation_build(client, idp, github, preset="replay-v2")
    await drive(app, build, runtime=runtime_for(app))
    for secret in ("hunter2", "AKIAABCDEFGHIJKLMNOP", "SECRET_TOKEN", "settings"):
        assert await find(owner, build, secret) == [], secret  # A07: blocked/excluded files are not indexed
    stranger, _, _ = await setup_project(client, idp, github)
    response = await stranger.get(f"/api/v1/builds/{build}/search", params={"q": "helper"})
    assert response.status_code == 404  # A04
