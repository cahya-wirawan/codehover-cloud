"""MCP sign-in: the app as the OAuth 2.1 authorization server of its private hosted MCP sites (0037).

An MCP client that gets 401 from `/mcp/<owner>/<project>` reads the protected-resource metadata (RFC 9728) named in
`WWW-Authenticate`, then the authorization server metadata (RFC 8414), registers itself (RFC 7591;
public clients only, no secret), and sends its person to `/oauth/authorize` with PKCE (S256 only) and
the site's URL as `resource` (RFC 8707). The app's consent page shows who asks for what; approving needs
access to the project and yields a one-time code (60 s), which the token endpoint trades for an access
token of one hour bound to that project and a refresh token of 30 days that changes on every use. A
replayed code or refresh token ends the grant. Codes and tokens are stored as SHA-256 digests only.

Browser steps redirect (errors as `?error=<code>`); the token, registration and revocation endpoints
answer in OAuth's own JSON error format, never the API envelope, and are never cached.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import exc, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ratelimit
from app.core.errors import ApiError
from app.core.settings import Settings
from app.core.tenancy import set_context
from app.modules.identity.sessions import CsrfPrincipal, CurrentPrincipal, Db
from app.modules.mcp.tokens import digest
from app.modules.published.router import find_site
from app.modules.workspaces.access import Member, ReadMember, WriteMember

API = "/api/v1"
SCOPE = "mcp:read"
ACCESS_PREFIX = "cho_"
REFRESH_PREFIX = "chr_"
CLIENT_PREFIX = "chc_"
ACCESS_SECONDS = 3600
REFRESH_DAYS = 30
CONSENT_PAGE = "/oauth/authorize"
# 0039: a site is "<owner>/<project>".
SITE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*/[a-z0-9]([a-z0-9-]{0,46}[a-z0-9])?$")
VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43}$")
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
# Schemes a browser must never be sent to; any other private-use scheme (RFC 8252 7.1) is a native app.
BLOCKED_SCHEMES = frozenset(
    {"http", "https", "javascript", "data", "file", "vbscript", "about", "blob", "ws", "wss"}
)
MAX_BODY_BYTES = 16 * 1024

well_known = APIRouter(tags=["oauth"])
router = APIRouter(tags=["oauth"])


# ---------------------------------------------------------------- addresses
def _origin(settings: Settings) -> str:
    return settings.public_origin.rstrip("/")


def resource_url(settings: Settings, site: str) -> str:
    return f"{_origin(settings)}{API}/mcp/{site}"


def metadata_url(settings: Settings, site: str) -> str:
    return f"{_origin(settings)}/.well-known/oauth-protected-resource{API}/mcp/{site}"


def challenge_header(settings: Settings, site: str, error: str | None = None) -> dict[str, str]:
    """`WWW-Authenticate` for a 401 from `/mcp/<owner>/<project>`: where the client learns how to sign in."""
    parts = ['Bearer realm="codehover"']
    if error:
        parts.append(f'error="{error}"')
    parts += [f'resource_metadata="{metadata_url(settings, site)}"', f'scope="{SCOPE}"']
    return {"WWW-Authenticate": ", ".join(parts)}


def site_of(settings: Settings, resource: str) -> str | None:
    """The "<owner>/<project>" an MCP resource URL names, or None."""
    prefix = f"{_origin(settings)}{API}/mcp/"
    if not resource.startswith(prefix):
        return None
    site = resource.removeprefix(prefix).rstrip("/")
    return site if SITE.fullmatch(site) and len(site.partition("/")[0]) <= 39 else None


def valid_redirect(uri: str) -> bool:
    if not isinstance(uri, str) or not uri or len(uri) > 512 or any(c.isspace() for c in uri):
        return False
    parts = urlsplit(uri)
    if parts.fragment or parts.username or parts.password:
        return False
    if parts.scheme == "https":
        return bool(parts.hostname)
    if parts.scheme == "http":
        return parts.hostname in LOOPBACK
    return (
        bool(re.fullmatch(r"[a-z][a-z0-9+.-]*", parts.scheme))
        and "." in parts.scheme
        and parts.scheme not in BLOCKED_SCHEMES
    )


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _oauth_error(error: str, description: str, status: int = 400) -> Response:
    return _no_store(JSONResponse({"error": error, "error_description": description}, status))


# ---------------------------------------------------------------- metadata
@well_known.get("/.well-known/oauth-protected-resource/api/v1/mcp/{owner}/{project}", include_in_schema=False)
async def protected_resource(owner: str, project: str, request: Request) -> JSONResponse:
    """The same answer for every well-formed address, published or not, so it reveals nothing."""
    slug = f"{owner}/{project}"
    if not SITE.fullmatch(slug):
        raise ApiError(404, "NOT_FOUND", "Not found.")
    settings = request.app.state.settings
    return JSONResponse(
        {
            "resource": resource_url(settings, slug),
            "authorization_servers": [_origin(settings)],
            "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"],
            "resource_name": "codehover site",
        }
    )


@well_known.get("/.well-known/oauth-authorization-server", include_in_schema=False)
async def authorization_server(request: Request) -> JSONResponse:
    origin = _origin(request.app.state.settings)
    return JSONResponse(
        {
            "issuer": origin,
            "authorization_endpoint": f"{origin}{API}/oauth/authorize",
            "token_endpoint": f"{origin}{API}/oauth/token",
            "registration_endpoint": f"{origin}{API}/oauth/register",
            "revocation_endpoint": f"{origin}{API}/oauth/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": [SCOPE],
            "authorization_response_iss_parameter_supported": True,
        }
    )


# ---------------------------------------------------------------- registration (RFC 7591)
@router.post("/oauth/register", include_in_schema=False)
async def register(request: Request, db: Db) -> Response:
    settings = request.app.state.settings
    await ratelimit.hit(
        request.app.state.rate_sessions, settings, "signin", ratelimit.client_address(request, settings)
    )
    raw = await request.body()
    try:
        if len(raw) > MAX_BODY_BYTES:
            raise ValueError
        body = json.loads(raw)
    except ValueError:
        return _oauth_error("invalid_client_metadata", "Send client metadata as JSON.")
    if not isinstance(body, dict):
        return _oauth_error("invalid_client_metadata", "Send client metadata as a JSON object.")
    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not 1 <= len(uris) <= 5 or not all(valid_redirect(u) for u in uris):
        return _oauth_error(
            "invalid_redirect_uri",
            "Give 1-5 redirect URIs: https, http on a loopback address, or a private-use scheme.",
        )
    if body.get("token_endpoint_auth_method", "none") != "none":
        return _oauth_error(
            "invalid_client_metadata", "Only public clients (no client secret) are supported."
        )
    grants = body.get("grant_types", ["authorization_code"])
    if not isinstance(grants, list) or not set(grants) <= {"authorization_code", "refresh_token"}:
        return _oauth_error(
            "invalid_client_metadata", "Only authorization_code and refresh_token are supported."
        )
    if body.get("response_types", ["code"]) != ["code"]:
        return _oauth_error("invalid_client_metadata", "Only the code response type is supported.")
    name = body.get("client_name")
    name = (name.strip() if isinstance(name, str) else "")[:100] or "MCP client"
    name = re.sub(r"[\x00-\x1f\x7f]", "", name) or "MCP client"
    client_id = CLIENT_PREFIX + secrets.token_urlsafe(24)
    created = (
        await db.execute(
            text("SELECT app_private.oauth_register_client(:c, :n, CAST(:u AS text[]))"),
            {"c": client_id, "n": name, "u": list(dict.fromkeys(uris))},
        )
    ).scalar_one()
    await db.commit()
    return _no_store(
        JSONResponse(
            {
                "client_id": client_id,
                "client_id_issued_at": int(created.timestamp()),
                "client_name": name,
                "redirect_uris": list(dict.fromkeys(uris)),
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
            201,
        )
    )


# ---------------------------------------------------------------- authorization (browser)
def _consent_redirect(**query: str) -> RedirectResponse:
    return RedirectResponse(f"{CONSENT_PAGE}?{urlencode(query)}", status_code=302)


def _client_redirect(settings: Settings, uri: str, **query: str | None) -> str:
    values = {k: v for k, v in query.items() if v is not None} | {"iss": _origin(settings)}
    return f"{uri}{'&' if '?' in uri else '?'}{urlencode(values)}"


@router.get("/oauth/authorize", include_in_schema=False)
async def authorize(
    request: Request,
    db: Db,
    client_id: Annotated[str, Query(max_length=100)] = "",
    redirect_uri: Annotated[str, Query(max_length=512)] = "",
    response_type: Annotated[str, Query(max_length=20)] = "",
    code_challenge: Annotated[str, Query(max_length=128)] = "",
    code_challenge_method: Annotated[str, Query(max_length=10)] = "",
    resource: Annotated[str, Query(max_length=600)] = "",
    scope: Annotated[str, Query(max_length=200)] = "",
    state: Annotated[str | None, Query(max_length=500)] = None,
) -> RedirectResponse:
    settings = request.app.state.settings
    try:
        await ratelimit.hit(
            request.app.state.rate_sessions, settings, "signin", ratelimit.client_address(request, settings)
        )
    except ApiError:
        return _consent_redirect(error="rate_limited")
    client = (
        await db.execute(text("SELECT * FROM app_private.oauth_client(:c)"), {"c": client_id})
    ).one_or_none()
    await db.commit()
    # An unknown client or a redirect it did not register: never redirect there (RFC 6749 4.1.2.1).
    if client is None or redirect_uri not in client.redirect_uris:
        return _consent_redirect(error="invalid_client")

    def refuse(error: str) -> RedirectResponse:
        return RedirectResponse(
            _client_redirect(settings, redirect_uri, error=error, state=state), status_code=302
        )

    if response_type != "code":
        return refuse("unsupported_response_type")
    if code_challenge_method != "S256" or not CHALLENGE.fullmatch(code_challenge):
        return refuse("invalid_request")
    if scope and SCOPE not in scope.split():
        return refuse("invalid_scope")
    slug = site_of(settings, resource)
    if slug is None:
        return refuse("invalid_target")
    request_id = (
        await db.execute(
            text("SELECT app_private.oauth_begin(:c, :r, :ch, :res, :s, :st)"),
            {
                "c": client.id,
                "r": redirect_uri,
                "ch": code_challenge,
                "res": resource,
                "s": slug,
                "st": state,
            },
        )
    ).scalar_one()
    await db.commit()
    return _consent_redirect(request=str(request_id))


async def _site(db: AsyncSession, site: str) -> Any:
    owner, _, project = site.partition("/")
    return await find_site(db, owner, project)


async def _pending(db: AsyncSession, request_id: UUID, user_id: UUID) -> tuple[Any, Any]:
    pending = (
        await db.execute(text("SELECT * FROM app_private.oauth_pending(:i)"), {"i": request_id})
    ).one_or_none()
    if pending is None:
        raise ApiError(404, "REQUEST_EXPIRED", "This sign-in request has expired. Start again from your app.")
    site = await _site(db, pending.site)
    allowed = None
    if site is not None:
        allowed = (
            await db.execute(
                text("SELECT app_private.project_role(:w, :p, :u)"),
                {"w": site.workspace_id, "p": site.project_id, "u": user_id},
            )
        ).scalar_one_or_none()
    if allowed is None:
        raise ApiError(404, "SITE_NOT_FOUND", "There is no site at this address that you can open.")
    return pending, site


class ConsentOut(BaseModel):
    client_name: str
    redirect_host: str
    site: str  # "<owner>/<project>" (0039)
    project: str
    workspace: str
    visibility: str
    scope: str = SCOPE


@router.get("/oauth/requests/{request_id}", response_model=ConsentOut)
async def consent(request_id: UUID, db: Db, principal: CurrentPrincipal) -> ConsentOut:
    pending, site = await _pending(db, request_id, principal.user_id)
    await set_context(db, workspace_id=site.workspace_id, user_id=principal.user_id)
    names = (
        await db.execute(
            text(
                "SELECT p.name AS project, w.name AS workspace FROM projects p JOIN workspaces w ON w.id = p.workspace_id"
                " WHERE p.workspace_id = :w AND p.id = :p"
            ),
            {"w": site.workspace_id, "p": site.project_id},
        )
    ).one()
    await db.commit()
    target = urlsplit(pending.redirect_uri)
    host = f"{target.scheme}://{target.netloc}" if target.netloc else f"{target.scheme}:"
    return ConsentOut(
        client_name=pending.client_name,
        redirect_host=host,
        site=f"{site.owner}/{site.project}",
        project=names.project,
        workspace=names.workspace,
        visibility=site.visibility,
    )


class DecisionIn(BaseModel):
    approve: bool


class DecisionOut(BaseModel):
    redirect_to: str


@router.post("/oauth/requests/{request_id}/decision", response_model=DecisionOut)
async def decide(
    request_id: UUID, body: DecisionIn, request: Request, db: Db, principal: CsrfPrincipal
) -> DecisionOut:
    settings = request.app.state.settings
    _, site = await _pending(db, request_id, principal.user_id)
    code = secrets.token_urlsafe(32)
    await set_context(db, workspace_id=site.workspace_id, user_id=principal.user_id)
    decided = (
        await db.execute(
            text("SELECT * FROM app_private.oauth_decide(:i, :w, :p, :a, :h)"),
            {
                "i": request_id,
                "w": site.workspace_id,
                "p": site.project_id,
                "a": body.approve,
                "h": digest(code) if body.approve else None,
            },
        )
    ).one_or_none()
    if decided is None:
        await db.rollback()
        raise ApiError(404, "REQUEST_EXPIRED", "This sign-in request has expired. Start again from your app.")
    if body.approve:
        await db.execute(
            text(
                "INSERT INTO audit_events(workspace_id, actor_user_id, action, resource_type, resource_id, metadata)"
                " VALUES (:w, :u, 'oauth.authorized', 'project', :p, '{}'::jsonb)"
            ),
            {"w": site.workspace_id, "u": principal.user_id, "p": site.project_id},
        )
    await db.commit()
    if body.approve:
        target = _client_redirect(settings, decided.redirect_uri, code=code, state=decided.state)
    else:
        target = _client_redirect(settings, decided.redirect_uri, error="access_denied", state=decided.state)
    return DecisionOut(redirect_to=target)


# ---------------------------------------------------------------- tokens
def pkce_matches(verifier: str, challenge: str) -> bool:
    if not VERIFIER.fullmatch(verifier):
        return False
    computed = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return secrets.compare_digest(computed, challenge)


async def _form(request: Request) -> dict[str, str] | None:
    if not request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
        return None
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return None
    try:
        parsed = parse_qs(
            raw.decode("ascii"), keep_blank_values=True, strict_parsing=False, max_num_fields=20
        )
    except (UnicodeDecodeError, ValueError):
        return None
    if any(len(v) != 1 for v in parsed.values()):
        return None  # a parameter sent twice (RFC 6749 3.1)
    return {k: v[0] for k, v in parsed.items()}


async def _issue(
    db: AsyncSession, grant_id: UUID, workspace_id: UUID, user_id: UUID, project_id: UUID, name: str
) -> str:
    token = ACCESS_PREFIX + secrets.token_urlsafe(32)
    await db.execute(
        text("DELETE FROM access_tokens WHERE workspace_id = :w AND grant_id = :g AND expires_at < now()"),
        {"w": workspace_id, "g": grant_id},
    )
    await db.execute(
        text(
            "INSERT INTO access_tokens(workspace_id, user_id, name, token_hash, prefix, expires_at, grant_id, project_id)"
            " VALUES (:w, :u, :n, :h, :p, :e, :g, :pr)"
        ),
        {
            "w": workspace_id,
            "u": user_id,
            "n": name[:80] or "MCP client",
            "h": digest(token),
            "p": token[: len(ACCESS_PREFIX) + 8],
            "e": datetime.now(UTC) + timedelta(seconds=ACCESS_SECONDS),
            "g": grant_id,
            "pr": project_id,
        },
    )
    return token


def _tokens(access: str, refresh: str) -> Response:
    return _no_store(
        JSONResponse(
            {
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": ACCESS_SECONDS,
                "refresh_token": refresh,
                "scope": SCOPE,
            }
        )
    )


async def _still_allowed(db: AsyncSession, workspace_id: UUID, project_id: UUID, user_id: UUID) -> bool:
    role = (
        await db.execute(
            text("SELECT app_private.project_role(:w, :p, :u)"),
            {"w": workspace_id, "p": project_id, "u": user_id},
        )
    ).scalar_one_or_none()
    return role is not None


@router.post("/oauth/token", include_in_schema=False)
async def token(request: Request, db: Db) -> Response:
    settings = request.app.state.settings
    await ratelimit.hit(
        request.app.state.rate_sessions, settings, "signin", ratelimit.client_address(request, settings)
    )
    form = await _form(request)
    if form is None:
        return _oauth_error("invalid_request", "Send the parameters form-encoded, each once.")
    client_id = form.get("client_id", "")
    resource = form.get("resource")
    if form.get("grant_type") == "authorization_code":
        found = (
            await db.execute(
                text("SELECT * FROM app_private.oauth_redeem(:h, :c, :r)"),
                {"h": digest(form.get("code", "")), "c": client_id, "r": form.get("redirect_uri", "")},
            )
        ).one_or_none()
        await db.commit()  # a redeemed (or replayed) code stays used even if what follows fails
        if found is None or not pkce_matches(form.get("code_verifier", ""), found.code_challenge):
            return _oauth_error("invalid_grant", "The code is not valid.")
        if resource is not None and resource.rstrip("/") != found.resource.rstrip("/"):
            return _oauth_error("invalid_target", "The resource is not the one this code was issued for.")
        await set_context(db, workspace_id=found.workspace_id, user_id=found.user_id)
        if not await _still_allowed(db, found.workspace_id, found.project_id, found.user_id):
            await db.rollback()
            return _oauth_error("invalid_grant", "The code is not valid.")
        refresh = REFRESH_PREFIX + secrets.token_urlsafe(32)
        grant_id = (
            await db.execute(
                text(
                    "INSERT INTO oauth_grants(workspace_id, user_id, project_id, client, client_name, authorization_id,"
                    " refresh_hash, refresh_expires_at, last_used_at)"
                    " VALUES (:w, :u, :p, :c, :n, :a, :h, now() + make_interval(days => :d), now()) RETURNING id"
                ),
                {
                    "w": found.workspace_id,
                    "u": found.user_id,
                    "p": found.project_id,
                    "c": found.client,
                    "n": found.client_name,
                    "a": found.authorization_id,
                    "h": digest(refresh),
                    "d": REFRESH_DAYS,
                },
            )
        ).scalar_one()
        access = await _issue(
            db, grant_id, found.workspace_id, found.user_id, found.project_id, found.client_name
        )
        await db.commit()
        return _tokens(access, refresh)
    if form.get("grant_type") == "refresh_token":
        old = digest(form.get("refresh_token", ""))
        found = (
            await db.execute(
                text("SELECT * FROM app_private.oauth_refresh_grant(:h, :c)"), {"h": old, "c": client_id}
            )
        ).one_or_none()
        await db.commit()  # a replay revoked the grant
        if found is None:
            return _oauth_error("invalid_grant", "The refresh token is not valid.")
        await set_context(db, workspace_id=found.workspace_id, user_id=found.user_id)
        if not await _still_allowed(db, found.workspace_id, found.project_id, found.user_id):
            await db.rollback()
            return _oauth_error("invalid_grant", "The refresh token is not valid.")
        refresh = REFRESH_PREFIX + secrets.token_urlsafe(32)
        try:
            rotated = (
                await db.execute(
                    text(
                        "UPDATE oauth_grants SET previous_refresh_hash = refresh_hash, refresh_hash = :new,"
                        " refresh_expires_at = now() + make_interval(days => :d), last_used_at = now()"
                        " WHERE workspace_id = :w AND id = :g AND refresh_hash = :old AND revoked_at IS NULL"
                        " RETURNING client_name"
                    ),
                    {
                        "new": digest(refresh),
                        "d": REFRESH_DAYS,
                        "w": found.workspace_id,
                        "g": found.grant_id,
                        "old": old,
                    },
                )
            ).one_or_none()
        except exc.IntegrityError:
            rotated = None
        if rotated is None:  # a concurrent refresh used it first
            await db.rollback()
            return _oauth_error("invalid_grant", "The refresh token is not valid.")
        access = await _issue(
            db, found.grant_id, found.workspace_id, found.user_id, found.project_id, rotated.client_name
        )
        await db.commit()
        return _tokens(access, refresh)
    return _oauth_error("unsupported_grant_type", "Use authorization_code or refresh_token.")


@router.post("/oauth/revoke", include_in_schema=False)
async def revoke(request: Request, db: Db) -> Response:
    """RFC 7009: always 200, whether or not the token was known."""
    settings = request.app.state.settings
    await ratelimit.hit(
        request.app.state.rate_sessions, settings, "signin", ratelimit.client_address(request, settings)
    )
    form = await _form(request)
    if form is None or not form.get("token"):
        return _oauth_error("invalid_request", "Send the token form-encoded.")
    await db.execute(text("SELECT app_private.oauth_revoke(:h)"), {"h": digest(form["token"])})
    await db.commit()
    return _no_store(Response(status_code=200))


# ---------------------------------------------------------------- connected apps (workspace page)
class GrantOut(BaseModel):
    id: UUID
    client_name: str
    project_id: UUID
    project: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class GrantList(BaseModel):
    items: list[GrantOut]
    next_cursor: None = None


@router.get("/workspaces/{workspace_id}/oauth-grants", response_model=GrantList)
async def list_grants(db: Db, member: ReadMember) -> GrantList:
    rows = await db.execute(
        text(
            "SELECT g.id, g.client_name, g.project_id, p.name AS project, g.created_at, g.last_used_at, g.revoked_at"
            " FROM oauth_grants g JOIN projects p ON p.workspace_id = g.workspace_id AND p.id = g.project_id"
            " WHERE g.workspace_id = :w AND g.user_id = :u ORDER BY g.created_at DESC LIMIT 100"
        ),
        {"w": member.workspace_id, "u": member.user_id},
    )
    items = [GrantOut(**dict(r._mapping)) for r in rows]
    await db.commit()
    return GrantList(items=items)


async def revoke_grant(db: AsyncSession, member: Member, grant_id: UUID) -> None:
    if member.support:
        raise ApiError(403, "FORBIDDEN", "Support access cannot revoke access.")
    row = (
        await db.execute(
            text(
                "UPDATE oauth_grants SET revoked_at = COALESCE(revoked_at, now())"
                " WHERE workspace_id = :w AND id = :g AND (user_id = :u OR :admin) RETURNING id"
            ),
            {"w": member.workspace_id, "g": grant_id, "u": member.user_id, "admin": member.at_least("admin")},
        )
    ).scalar_one_or_none()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Connected app not found.")
    await db.execute(
        text(
            "INSERT INTO audit_events(workspace_id, actor_user_id, action, resource_type, resource_id, metadata)"
            " VALUES (:w, :u, 'oauth.revoked', 'oauth_grant', :g, '{}'::jsonb)"
        ),
        {"w": member.workspace_id, "u": member.user_id, "g": grant_id},
    )


@router.delete("/workspaces/{workspace_id}/oauth-grants/{oauth_grant_id}", status_code=204)
async def delete_grant(oauth_grant_id: UUID, db: Db, member: WriteMember) -> Response:
    await revoke_grant(db, member, oauth_grant_id)
    await db.commit()
    return Response(status_code=204)


async def revoke_for_member(db: AsyncSession, workspace_id: UUID, user_id: UUID) -> None:
    await db.execute(
        text(
            "UPDATE oauth_grants SET revoked_at = now() WHERE workspace_id = :w AND user_id = :u AND revoked_at IS NULL"
        ),
        {"w": workspace_id, "u": user_id},
    )
