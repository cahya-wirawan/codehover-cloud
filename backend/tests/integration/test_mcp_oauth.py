"""MCP sign-in (0037; 06 "/oauth", 09): OAuth 2.1 with PKCE for private hosted MCP sites.

The MCP client is played by a cookie-less client; the person who consents by the signed-in owner.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tests.integration.auth_support import ORIGIN, FakeIdP, browser, ddl, unsafe
from tests.integration.conftest import needs_db
from tests.integration.test_github import FakeGitHub, github  # noqa: F401
from tests.integration.test_mcp import Mcp
from tests.integration.test_publication import address, publish, ready_build
from tests.integration.test_workspaces import NewUser, new_user  # noqa: F401

pytestmark = [pytest.mark.integration, needs_db]

REDIRECT = "http://127.0.0.1:33418/callback"
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


async def register(app_client: httpx.AsyncClient, **body: Any) -> httpx.Response:
    return await app_client.post(
        "/api/v1/oauth/register", json={"client_name": "Claude Code", "redirect_uris": [REDIRECT], **body}
    )


async def authorize(
    app_client: httpx.AsyncClient, client_id: str, slug: str, challenge: str, **extra: str
) -> httpx.Response:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": f"{ORIGIN}/api/v1/mcp/{slug}",
        "scope": "mcp:read",
        "state": "s-123",
        **extra,
    }
    return await app_client.get("/api/v1/oauth/authorize", params=params)


async def consent(
    person: httpx.AsyncClient, authorized: httpx.Response, approve: bool = True
) -> dict[str, str]:
    assert authorized.status_code == 302, authorized.text
    location = authorized.headers["location"]
    assert location.startswith("/oauth/authorize?request=")
    request_id = query(location)["request"]
    decided = await unsafe(
        person, "POST", f"/api/v1/oauth/requests/{request_id}/decision", json={"approve": approve}
    )
    assert decided.status_code == 200, decided.text
    target = decided.json()["redirect_to"]
    assert target.startswith(REDIRECT + "?")
    return query(target)


async def token(app_client: httpx.AsyncClient, **form: str) -> httpx.Response:
    return await app_client.post("/api/v1/oauth/token", data=form)


async def signed_in(
    app_client: httpx.AsyncClient, person: httpx.AsyncClient, client_id: str, slug: str
) -> dict[str, Any]:
    verifier, challenge = pkce()
    code = (await consent(person, await authorize(app_client, client_id, slug, challenge)))["code"]
    issued = await token(
        app_client,
        grant_type="authorization_code",
        code=code,
        redirect_uri=REDIRECT,
        client_id=client_id,
        code_verifier=verifier,
        resource=f"{ORIGIN}/api/v1/mcp/{slug}",
    )
    assert issued.status_code == 200, issued.text
    assert issued.headers["cache-control"] == "no-store"
    body: dict[str, Any] = issued.json()
    return body


async def test_discovery_and_registration(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    _owner, _ws, _project, _build, app = await ready_build(client, idp, github)
    async with browser(app) as mcp_client:
        server = (await mcp_client.get("/.well-known/oauth-authorization-server")).json()
        assert server["issuer"] == ORIGIN
        assert server["code_challenge_methods_supported"] == ["S256"]
        assert server["token_endpoint"] == f"{ORIGIN}/api/v1/oauth/token"
        assert server["registration_endpoint"] == f"{ORIGIN}/api/v1/oauth/register"
        resource = (
            await mcp_client.get("/.well-known/oauth-protected-resource/api/v1/mcp/anyone/any-site")
        ).json()
        assert resource == {
            "resource": f"{ORIGIN}/api/v1/mcp/anyone/any-site",
            "authorization_servers": [ORIGIN],
            "scopes_supported": ["mcp:read"],
            "bearer_methods_supported": ["header"],
            "resource_name": "codehover site",
        }
        registered = await register(mcp_client)
        assert registered.status_code == 201, registered.text
        assert registered.json()["client_id"].startswith("chc_")
        assert registered.json()["token_endpoint_auth_method"] == "none"
        native = await register(
            mcp_client, redirect_uris=["cursor.app://oauth/callback", "https://x.test/cb"]
        )
        assert native.status_code == 201, native.text
        for bad in (
            ["http://evil.test/callback"],
            ["javascript:alert(1)"],
            ["https://x.test/cb#frag"],
            ["https://user:pw@x.test/cb"],
            [],
            ["https://x.test/cb"] * 6,
        ):
            refused = await register(mcp_client, redirect_uris=bad)
            assert refused.status_code == 400 and refused.json()["error"] == "invalid_redirect_uri", bad
        secret = await register(mcp_client, token_endpoint_auth_method="client_secret_basic")
        assert secret.status_code == 400 and secret.json()["error"] == "invalid_client_metadata"


async def test_a_member_signs_an_mcp_client_in_to_a_private_site(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, new_user: NewUser
) -> None:
    owner, ws, project, build, app = await ready_build(client, idp, github)
    slug = await address(owner, project)
    assert (
        await publish(owner, project, build_id=build, visibility="private", public_slug=slug)
    ).status_code == 200
    async with browser(app) as mcp_client:
        client_id = (await register(mcp_client)).json()["client_id"]
        verifier, challenge = pkce()

        # Requests that cannot be trusted never redirect to the client.
        unknown = await authorize(mcp_client, "chc_" + "x" * 30, slug, challenge)
        assert (
            unknown.status_code == 302
            and unknown.headers["location"] == "/oauth/authorize?error=invalid_client"
        )
        foreign = await authorize(mcp_client, client_id, slug, challenge, redirect_uri="http://127.0.0.1:1/x")
        assert foreign.headers["location"] == "/oauth/authorize?error=invalid_client"
        # Once the redirect is known, errors go back to the client with its state.
        for extra, error in (
            ({"code_challenge_method": "plain"}, "invalid_request"),
            ({"code_challenge": "short"}, "invalid_request"),
            ({"resource": "https://elsewhere.test/api/v1/mcp/x"}, "invalid_target"),
            ({"scope": "admin"}, "invalid_scope"),
            ({"response_type": "token"}, "unsupported_response_type"),
        ):
            refused = query(
                (await authorize(mcp_client, client_id, slug, challenge, **extra)).headers["location"]
            )
            assert refused == {"error": error, "state": "s-123", "iss": ORIGIN}, extra

        # The consent page shows what is asked, only to someone who may see the site.
        pending = await authorize(mcp_client, client_id, slug, challenge)
        request_id = query(pending.headers["location"])["request"]
        outsider = await new_user()
        assert (await outsider.get(f"/api/v1/oauth/requests/{request_id}")).json()["error"][
            "code"
        ] == "SITE_NOT_FOUND"
        refused = await unsafe(
            outsider, "POST", f"/api/v1/oauth/requests/{request_id}/decision", json={"approve": True}
        )
        assert refused.status_code == 404
        shown = (await owner.get(f"/api/v1/oauth/requests/{request_id}")).json()
        assert shown == {
            "client_name": "Claude Code",
            "client_url": None,  # registered, not named by a metadata document (0043)
            "redirect_host": "http://127.0.0.1:33418",
            "site": slug,
            "project": "API",
            "workspace": shown["workspace"],
            "visibility": "private",
            "scope": "mcp:read",
        }
        answered = await consent(owner, pending)
        assert answered["state"] == "s-123" and answered["iss"] == ORIGIN
        again = await unsafe(
            owner, "POST", f"/api/v1/oauth/requests/{request_id}/decision", json={"approve": True}
        )
        assert again.status_code == 404  # decided once

        wrong_verifier = await token(
            mcp_client,
            grant_type="authorization_code",
            code=answered["code"],
            redirect_uri=REDIRECT,
            client_id=client_id,
            code_verifier=pkce()[0],
        )
        assert wrong_verifier.status_code == 400 and wrong_verifier.json()["error"] == "invalid_grant"
        burned = await token(
            mcp_client,
            grant_type="authorization_code",
            code=answered["code"],
            redirect_uri=REDIRECT,
            client_id=client_id,
            code_verifier=verifier,
        )
        assert burned.json()["error"] == "invalid_grant"  # a code is tried once

        issued = await signed_in(mcp_client, owner, client_id, slug)
        assert (
            issued["token_type"] == "Bearer"
            and issued["expires_in"] == 3600
            and issued["scope"] == "mcp:read"
        )
        access, refresh = issued["access_token"], issued["refresh_token"]
        assert access.startswith("cho_") and refresh.startswith("chr_")
        assert access not in str(ddl("SELECT * FROM access_tokens WHERE workspace_id = :w", w=ws))
        mcp = Mcp(mcp_client, slug, access)
        overview, failed = await mcp.tool("overview")
        assert not failed and overview.startswith("# API")

        # Bound to this site: another published site of the workspace refuses it.
        _o2, _w2, other_project, other_build, _a2 = await ready_build(client, idp, github)
        other_slug = await address(_o2, other_project)
        assert (
            await publish(
                _o2,
                other_project,
                build_id=other_build,
                visibility="public",
                public_slug=other_slug,
                confirmation="API",
            )
        ).status_code == 200
        elsewhere = await Mcp(mcp_client, other_slug, access).post(PING)
        assert (
            elsewhere.status_code == 401 and 'error="invalid_token"' in elsewhere.headers["www-authenticate"]
        )
        assert (await Mcp(mcp_client, other_slug).post(PING)).status_code == 200  # anonymous is fine there

        # Refreshing rotates; replaying the old refresh token ends the grant.
        refreshed = await token(
            mcp_client, grant_type="refresh_token", refresh_token=refresh, client_id=client_id
        )
        assert refreshed.status_code == 200, refreshed.text
        newer = refreshed.json()
        assert newer["refresh_token"] != refresh
        assert (await Mcp(mcp_client, slug, newer["access_token"]).post(PING)).status_code == 200
        other_client = (await register(mcp_client, client_name="Other")).json()["client_id"]
        assert (
            await token(
                mcp_client,
                grant_type="refresh_token",
                refresh_token=newer["refresh_token"],
                client_id=other_client,
            )
        ).json()["error"] == "invalid_grant"  # another client cannot use it
        replayed = await token(
            mcp_client, grant_type="refresh_token", refresh_token=refresh, client_id=client_id
        )
        assert replayed.status_code == 400 and replayed.json()["error"] == "invalid_grant"
        assert (await Mcp(mcp_client, slug, newer["access_token"]).post(PING)).status_code == 401
        assert (
            await token(
                mcp_client,
                grant_type="refresh_token",
                refresh_token=newer["refresh_token"],
                client_id=client_id,
            )
        ).status_code == 400

        # Connected apps on the workspace page; personal tokens are listed apart.
        kept = await signed_in(mcp_client, owner, client_id, slug)
        grants = (await owner.get(f"/api/v1/workspaces/{ws}/oauth-grants")).json()["items"]
        live = [g for g in grants if g["revoked_at"] is None]
        assert [(g["client_name"], g["project"]) for g in live] == [("Claude Code", "API")]
        assert (await owner.get(f"/api/v1/workspaces/{ws}/access-tokens")).json()["items"] == []
        removed = await unsafe(owner, "DELETE", f"/api/v1/workspaces/{ws}/oauth-grants/{live[0]['id']}")
        assert removed.status_code == 204
        assert (await Mcp(mcp_client, slug, kept["access_token"]).post(PING)).status_code == 401

        # RFC 7009 revocation by whoever holds the token.
        held = await signed_in(mcp_client, owner, client_id, slug)
        assert (
            await mcp_client.post("/api/v1/oauth/revoke", data={"token": held["refresh_token"]})
        ).status_code == 200
        assert (await Mcp(mcp_client, slug, held["access_token"]).post(PING)).status_code == 401
        assert (await mcp_client.post("/api/v1/oauth/revoke", data={"token": "unknown"})).status_code == 200

        # Denying sends the client away with access_denied.
        _v, denied_challenge = pkce()
        denied = await consent(
            owner, await authorize(mcp_client, client_id, slug, denied_challenge), approve=False
        )
        assert denied == {"error": "access_denied", "state": "s-123", "iss": ORIGIN}

    actions = {r[0] for r in ddl("SELECT action FROM audit_events WHERE workspace_id = :w", w=ws)}
    assert {"oauth.authorized", "oauth.revoked"} <= actions


async def test_losing_access_ends_mcp_sign_in(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    owner, ws, project, build, app = await ready_build(client, idp, github)
    slug = await address(owner, project)
    assert (
        await publish(owner, project, build_id=build, visibility="private", public_slug=slug)
    ).status_code == 200
    async with browser(app) as mcp_client:
        client_id = (await register(mcp_client)).json()["client_id"]
        issued = await signed_in(mcp_client, owner, client_id, slug)
        assert (await Mcp(mcp_client, slug, issued["access_token"]).post(PING)).status_code == 200
        ddl("UPDATE workspaces SET status = 'suspended' WHERE id = :w", w=ws)
        assert (await Mcp(mcp_client, slug, issued["access_token"]).post(PING)).status_code == 401
        ddl("UPDATE workspaces SET status = 'active' WHERE id = :w", w=ws)
        assert (await Mcp(mcp_client, slug, issued["access_token"]).post(PING)).status_code == 200
        ddl(
            "UPDATE access_tokens SET created_at = now() - interval '2 hours', expires_at = now() - interval '1 hour' WHERE grant_id IS NOT NULL AND workspace_id = :w",
            w=ws,
        )
        assert (await Mcp(mcp_client, slug, issued["access_token"]).post(PING)).status_code == 401
        refreshed = await token(
            mcp_client, grant_type="refresh_token", refresh_token=issued["refresh_token"], client_id=client_id
        )
        assert refreshed.status_code == 200  # an expired access token is replaced
        assert (
            ddl("SELECT count(*) FROM access_tokens WHERE workspace_id = :w AND grant_id IS NOT NULL", w=ws)[
                0
            ][0]
            == 1
        )  # the expired one was removed


class FakeDocuments:
    """Client ID metadata documents by URL; counts fetches; a missing URL fails like an unreachable host."""

    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.fetched: list[str] = []

    async def fetch(self, url: str) -> dict[str, Any]:
        from app.adapters.oauth.client_metadata import MetadataError

        self.fetched.append(url)
        if url not in self.documents:
            raise MetadataError("unreachable")
        return self.documents[url]


async def test_a_client_named_by_its_metadata_document_signs_in(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub
) -> None:
    """0043 (MCP 2025-11-25): an https client_id is the client's metadata document, fetched at sign-in,
    kept an hour, and used only when it names itself and the redirect URI."""
    owner, _ws, project, build, app = await ready_build(client, idp, github)
    slug = await address(owner, project)
    assert (await publish(owner, project, build_id=build)).status_code == 200
    documents = FakeDocuments()
    app.state.client_metadata = documents
    url = "https://client.example.test/oauth/client.json"
    documents.documents[url] = {
        "client_id": url,
        "client_name": "Example Client",
        "redirect_uris": [REDIRECT],
    }
    async with browser(app) as mcp_client:
        server = (await mcp_client.get("/.well-known/oauth-authorization-server")).json()
        assert server["client_id_metadata_document_supported"] is True
        # The consent page names the document, since the name is the client's own claim.
        _verifier, challenge = pkce()
        authorized = await authorize(mcp_client, url, slug, challenge)
        assert authorized.status_code == 302 and authorized.headers["location"].startswith(
            "/oauth/authorize?request="
        )
        pending = (
            await owner.get(f"/api/v1/oauth/requests/{query(authorized.headers['location'])['request']}")
        ).json()
        assert pending["client_name"] == "Example Client" and pending["client_url"] == url
        issued = await signed_in(mcp_client, owner, url, slug)
        assert (await Mcp(mcp_client, slug, issued["access_token"]).post(PING)).status_code == 200
        assert documents.fetched == [url]  # kept: the second sign-in did not fetch again
        # A document for another URL, or a redirect it does not list, is refused without redirecting there.
        other = "https://client.example.test/other.json"
        documents.documents[other] = {"client_id": url, "redirect_uris": [REDIRECT]}
        assert query((await authorize(mcp_client, other, slug, challenge)).headers["location"]) == {
            "error": "invalid_client"
        }
        wrong = await authorize(mcp_client, url, slug, challenge, redirect_uri="http://127.0.0.1:9/elsewhere")
        assert query(wrong.headers["location"]) == {"error": "invalid_client"}
        # After an hour it is fetched again; an unreachable host keeps the stored copy for a day.
        ddl("UPDATE oauth_clients SET fetched_at = now() - interval '2 hours' WHERE client_id = :c", c=url)
        del documents.documents[url]
        assert (
            (await authorize(mcp_client, url, slug, challenge))
            .headers["location"]
            .startswith("/oauth/authorize?request=")
        )
        ddl("UPDATE oauth_clients SET fetched_at = now() - interval '25 hours' WHERE client_id = :c", c=url)
        assert query((await authorize(mcp_client, url, slug, challenge)).headers["location"]) == {
            "error": "invalid_client"
        }
        # Unknown hosts are never registered.
        unknown = "https://nobody.example.test/client.json"
        assert query((await authorize(mcp_client, unknown, slug, challenge)).headers["location"]) == {
            "error": "invalid_client"
        }
        assert ddl("SELECT count(*) FROM oauth_clients WHERE client_id = :c", c=unknown) == [(0,)]
