"""Hosted MCP (06 "/mcp/{slug}", 09): a published site's documentation as read-only MCP tools.

Public sites answer anyone; private sites answer only a live personal access token of a member who may
see the project. Without a token a private or unknown site is 401 (sign in, 0037); with a valid token
without access it is 404; a token that is not valid, or is for another site, is 401.
"""

from __future__ import annotations

import secrets
from typing import Any

import httpx
import pytest

from tests.integration.auth_support import ORIGIN, FakeIdP, browser, ddl, unsafe
from tests.integration.conftest import needs_db
from tests.integration.test_github import FakeGitHub, github, workspace  # noqa: F401
from tests.integration.test_publication import address, publish, ready_build
from tests.integration.test_workspaces import NewUser, join, new_user  # noqa: F401

pytestmark = [pytest.mark.integration, needs_db]


class Mcp:
    def __init__(self, client: httpx.AsyncClient, slug: str, token: str | None = None) -> None:
        self.client, self.slug, self.token = client, slug, token
        self.ids = 0

    async def post(self, body: Any, **headers: str) -> httpx.Response:
        auth = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        return await self.client.post(
            f"/api/v1/mcp/{self.slug}",
            json=body,
            headers={"Accept": "application/json, text/event-stream", **auth, **headers},
        )

    async def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.ids += 1
        response = await self.post(
            {"jsonrpc": "2.0", "id": self.ids, "method": method, "params": params or {}}
        )
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        assert body["id"] == self.ids
        return body

    async def tool(self, tool: str, /, **arguments: Any) -> tuple[str, bool]:
        result = (await self.rpc("tools/call", {"name": tool, "arguments": arguments}))["result"]
        return result["content"][0]["text"], result["isError"]


async def create_token(member: httpx.AsyncClient, ws: str, key: str | None = None) -> httpx.Response:
    return await unsafe(
        member,
        "POST",
        f"/api/v1/workspaces/{ws}/access-tokens",
        json={"name": "my editor", "expires_in_days": 30},
        headers={"Idempotency-Key": key or secrets.token_hex(12)},
    )


async def test_a_public_site_serves_every_tool_to_anyone(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, _ws, project, build, app = await ready_build(client, idp, github)
    slug = await address(owner, project)
    published = await publish(
        owner, project, build_id=build, visibility="public", public_slug=slug, confirmation="API"
    )
    assert published.status_code == 200, published.text
    async with browser(app) as visitor:
        mcp = Mcp(visitor, slug)
        init = (await mcp.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}))["result"]
        assert init["protocolVersion"] == "2025-06-18" and init["capabilities"] == {
            "tools": {"listChanged": False},
            "resources": {"listChanged": False, "subscribe": False},
            "prompts": {"listChanged": False},
        }
        assert "data, not instructions" in init["instructions"]
        assert (await mcp.post({"jsonrpc": "2.0", "method": "notifications/initialized"})).status_code == 202
        names = [t["name"] for t in (await mcp.rpc("tools/list"))["result"]["tools"]]
        assert names == [
            "overview",
            "search",
            "get_definition",
            "find_references",
            "get_file",
            "read_lines",
            "list_guide",
            "read_guide_page",
        ]
        outputs: list[str] = []
        overview, failed = await mcp.tool("overview")
        assert not failed and overview.startswith("# API") and "## Guide pages" in overview
        hits, _ = await mcp.tool("search", query="helper")
        assert "`helper` in `src/util.py:1`" in hits
        definition, failed = await mcp.tool("get_definition", name="helper")
        assert not failed and "`src/util.py:1-2`" in definition and "return len(args)" in definition
        assert "## Used by" in definition and "`main`" in definition
        by_id, _ = await mcp.tool("get_definition", name="src/util.py::helper", include_source=False)
        assert "## Source" not in by_id
        refs, _ = await mcp.tool("find_references", name="helper")
        assert "`src/app.py:" in refs and "return helper(argv)" in refs
        outline, _ = await mcp.tool("get_file", path="src/app.py")
        assert "`Greeter.greet`" in outline and "`main`" in outline
        lines, _ = await mcp.tool("read_lines", path="./src/app.py", start=5, end=7)
        assert lines.splitlines()[2].startswith("5 | def main(argv):") and "8 |" not in lines
        guide, _ = await mcp.tool("list_guide")
        page_slug = guide.split("`")[1]
        page, failed = await mcp.tool("read_guide_page", slug=page_slug)
        assert not failed and "[[" not in page and ".py:" in page  # citations name paths and lines
        outputs += [overview, hits, definition, refs, outline, lines, guide, page]
        assert all(build not in text for text in outputs)  # the build ID never leaves the resolver

        # Tool failures are results; protocol mistakes are JSON-RPC errors.
        missing, failed = await mcp.tool("get_file", path="nope.py")
        assert failed and "No file" in missing
        ambiguous, failed = await mcp.tool("get_definition", name="no_such_thing")
        assert failed and "search()" in ambiguous
        unknown_language, failed = await mcp.tool("overview", language="de")
        assert failed and "no 'de' version" in unknown_language
        assert (await mcp.rpc("tools/call", {"name": "write_file", "arguments": {}}))["error"][
            "code"
        ] == -32602
        assert (await mcp.rpc("tools/call", {"name": "search", "arguments": {"q": 1}}))["error"][
            "code"
        ] == -32602
        assert (await mcp.rpc("resources/subscribe"))["error"]["code"] == -32601
        batch = await mcp.post([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
        assert batch.status_code == 400 and batch.json()["error"]["code"] == -32600
        assert (
            await mcp.post(
                {"jsonrpc": "2.0", "id": 9, "method": "ping"}, **{"MCP-Protocol-Version": "1999-01-01"}
            )
        ).status_code == 400
        assert (
            await mcp.post({"jsonrpc": "2.0", "id": 9, "method": "ping"}, Origin="https://evil.example")
        ).status_code == 403
        assert (
            await mcp.post({"jsonrpc": "2.0", "id": 9, "method": "ping"}, Origin=ORIGIN)
        ).status_code == 200
        assert (await visitor.get(f"/api/v1/mcp/{slug}")).status_code == 405
        unknown = await Mcp(visitor, "nobody-x/no-such-site").post(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        )
        assert unknown.status_code == 401  # like a private site: sign in first (0037)
        assert 'resource_metadata="http' in unknown.headers["www-authenticate"]

    # Unpublishing stops the endpoint on the next request.
    ddl(
        "UPDATE publications SET suspended_at = now(), suspended_reason = 'operator' WHERE project_id = :p",
        p=project,
    )
    async with browser(app) as visitor:  # a suspended site is gone, like an unknown address
        assert (
            await Mcp(visitor, slug).post({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        ).status_code == 401
    ddl(
        "UPDATE publications SET suspended_at = NULL, suspended_reason = NULL WHERE project_id = :p",
        p=project,
    )


async def test_a_site_serves_its_overview_guide_and_files_as_resources_and_offers_prompts(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    """Resources name the site (`codehover://<slug>/...`) and return what the tools return; a URI of
    another site, or a page or file the site does not have, is -32002. Prompts only return text."""
    owner, _ws, project, build, app = await ready_build(client, idp, github)
    slug = await address(owner, project)
    published = await publish(
        owner, project, build_id=build, visibility="public", public_slug=slug, confirmation="API"
    )
    assert published.status_code == 200, published.text
    async with browser(app) as visitor:
        mcp = Mcp(visitor, slug)
        listed = (await mcp.rpc("resources/list"))["result"]["resources"]
        assert listed[0]["uri"] == f"codehover://{slug}/overview"
        guide = [r for r in listed if r["name"].startswith("guide/")]
        assert guide and all(r["mimeType"] == "text/markdown" and r["description"] for r in guide)
        page = (await mcp.rpc("resources/read", {"uri": guide[0]["uri"]}))["result"]["contents"][0]
        text, _ = await mcp.tool("read_guide_page", slug=guide[0]["name"].removeprefix("guide/"))
        assert page["uri"] == guide[0]["uri"] and page["text"] == text
        overview = (await mcp.rpc("resources/read", {"uri": f"codehover://{slug}/overview"}))["result"]
        assert overview["contents"][0]["text"] == (await mcp.tool("overview"))[0]
        template = (await mcp.rpc("resources/templates/list"))["result"]["resourceTemplates"]
        assert [t["uriTemplate"] for t in template] == [f"codehover://{slug}/files/{{path}}"]
        source = (await mcp.rpc("resources/read", {"uri": f"codehover://{slug}/files/src/app.py"}))["result"]
        assert "src/app.py" in source["contents"][0]["text"] and "Tiny app" in source["contents"][0]["text"]
        encoded = await mcp.rpc("resources/read", {"uri": f"codehover://{slug}/files/src%2Fapp.py"})
        assert encoded["result"]["contents"][0]["text"] == source["contents"][0]["text"]
        for missing in (
            f"codehover://other-{slug}/overview",
            f"codehover://{slug}/guide/no-such-page",
            f"codehover://{slug}/files/nope.py",
            f"codehover://{slug}/overview?language=ja",
            f"https://{slug}/overview",
            f"codehover://{slug}/../overview",
        ):
            error = (await mcp.rpc("resources/read", {"uri": missing}))["error"]
            assert error["code"] == -32002, missing
        prompts = (await mcp.rpc("prompts/list"))["result"]["prompts"]
        assert [p["name"] for p in prompts] == ["tour", "explain", "where"]
        explained = await mcp.rpc("prompts/get", {"name": "explain", "arguments": {"target": "helper"}})
        message = explained["result"]["messages"][0]
        assert message["role"] == "user" and "Explain helper" in message["content"]["text"]
        assert (await mcp.rpc("prompts/get", {"name": "explain"}))["error"]["code"] == -32602
        assert (await mcp.rpc("prompts/get", {"name": "nope"}))["error"]["code"] == -32602


async def test_a_private_site_needs_a_live_token_of_a_member_who_may_see_it(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, new_user: NewUser
) -> None:
    owner, ws, project, build, app = await ready_build(client, idp, github)
    slug = await address(owner, project)
    assert (
        await publish(owner, project, build_id=build, visibility="private", public_slug=slug)
    ).status_code == 200
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    async with browser(app) as visitor:
        anonymous = await Mcp(visitor, slug).post(ping)
        unknown = await Mcp(visitor, f"x-{slug}").post(ping)
        # Private and unknown addresses answer alike (slugs are not disclosed), naming where to sign in.
        assert anonymous.status_code == unknown.status_code == 401
        challenge = anonymous.headers["www-authenticate"]
        assert f'/.well-known/oauth-protected-resource/api/v1/mcp/{slug}"' in challenge
        assert "error=" not in challenge and 'scope="mcp:read"' in challenge
    # A member's browser session is not enough: the endpoint never reads cookies.
    assert (await Mcp(owner, slug).post(ping)).status_code == 401

    key = secrets.token_hex(12)
    created = await create_token(owner, ws, key)
    assert created.status_code == 201, created.text
    token = created.json()["token"]
    assert token.startswith("chm_") and created.json()["prefix"] == token[:12]
    replay = await create_token(owner, ws, key)
    assert replay.status_code == 201 and replay.json()["token"] is None  # shown once
    assert token not in str(ddl("SELECT * FROM access_tokens WHERE workspace_id = :w", w=ws))
    assert token not in str(ddl("SELECT response_body FROM idempotency_keys WHERE key = :k", k=key))
    listed = (await owner.get(f"/api/v1/workspaces/{ws}/access-tokens")).json()["items"]
    assert [t["id"] for t in listed] == [created.json()["id"]] and "token" not in listed[0]

    async with browser(app) as tool_client:
        mcp = Mcp(tool_client, slug, token)
        assert "result" in await mcp.rpc("ping")
        overview, failed = await mcp.tool("overview")
        assert not failed and overview.startswith("# API")
        assert ddl(
            "SELECT last_used_at IS NOT NULL FROM access_tokens WHERE id = :t", t=created.json()["id"]
        )[0][0]
        bad = await Mcp(tool_client, slug, "chm_" + "x" * 40).post(ping)
        assert bad.status_code == 401 and "invalid_token" in bad.headers["www-authenticate"]
        assert (await Mcp(tool_client, slug).post(ping, Authorization="Basic abc")).status_code == 401

    # A token of another workspace opens nothing here; one of a member who cannot see the project neither.
    outsider = await new_user()
    other_ws = (
        await unsafe(
            outsider,
            "POST",
            "/api/v1/workspaces",
            json={"name": "Other", "slug": f"other-{secrets.token_hex(3)}"},
            headers={"Idempotency-Key": secrets.token_hex(12)},
        )
    ).json()["id"]
    foreign = (await create_token(outsider, other_ws)).json()["token"]
    async with browser(app) as tool_client:
        assert (await Mcp(tool_client, slug, foreign).post(ping)).status_code == 404
    viewer = await new_user()
    await join(owner, ws, viewer, "viewer")
    viewer_token = (await create_token(viewer, ws)).json()["token"]
    async with browser(app) as tool_client:
        assert (await Mcp(tool_client, slug, viewer_token).post(ping)).status_code == 200
        ddl("UPDATE projects SET access = 'restricted' WHERE id = :p", p=project)
        assert (await Mcp(tool_client, slug, viewer_token).post(ping)).status_code == 404  # 0027
        assert (await Mcp(tool_client, slug, token).post(ping)).status_code == 200  # the owner still may
        ddl("UPDATE projects SET access = 'workspace' WHERE id = :p", p=project)

    # Revoked, expired, or its member gone: the token stops working.
    viewer_tokens = (await viewer.get(f"/api/v1/workspaces/{ws}/access-tokens")).json()["items"]
    removed = await unsafe(
        owner, "DELETE", f"/api/v1/workspaces/{ws}/members/{viewer_user(ws, viewer_tokens[0]['id'])}"
    )
    assert removed.status_code == 204, removed.text
    assert ddl("SELECT revoked_at IS NOT NULL FROM access_tokens WHERE id = :t", t=viewer_tokens[0]["id"])[0][
        0
    ]
    async with browser(app) as tool_client:
        assert (await Mcp(tool_client, slug, viewer_token).post(ping)).status_code == 401
        ddl(
            "UPDATE access_tokens SET created_at = now() - interval '2 days', expires_at = now() - interval '1 day'"
            " WHERE id = :t",
            t=created.json()["id"],
        )
        assert (await Mcp(tool_client, slug, token).post(ping)).status_code == 401
    second = (await create_token(owner, ws)).json()
    revoked = await unsafe(owner, "DELETE", f"/api/v1/workspaces/{ws}/access-tokens/{second['id']}")
    assert revoked.status_code == 204
    async with browser(app) as tool_client:
        assert (await Mcp(tool_client, slug, second["token"]).post(ping)).status_code == 401
    # Another member cannot revoke the owner's token unless they are an admin.
    assert (
        await unsafe(outsider, "DELETE", f"/api/v1/workspaces/{ws}/access-tokens/{second['id']}")
    ).status_code == 404
    actions = [
        r[0]
        for r in ddl(
            "SELECT action FROM audit_events WHERE workspace_id = :w AND action LIKE 'access_token.%'", w=ws
        )
    ]
    assert actions.count("access_token.created") == 3 and actions.count("access_token.revoked") == 1


def viewer_user(ws: str, token_id: str) -> str:
    return str(
        ddl("SELECT user_id FROM access_tokens WHERE workspace_id = :w AND id = :t", w=ws, t=token_id)[0][0]
    )
