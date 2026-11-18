"""Auth and account endpoints (06 "Endpoint inventory": /auth/*, /me*)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import text

from app.adapters.oauth import OAuthError, OAuthProvider
from app.core import metrics, ratelimit
from app.core.errors import ApiError
from app.core.security import safe_return_path
from app.core.settings import Settings
from app.modules.identity import service
from app.modules.identity.service import AuthFlowError
from app.modules.identity.sessions import (
    CsrfPrincipal,
    CurrentPrincipal,
    Db,
    OptionalPrincipal,
    clear_flow_cookie,
    clear_session_cookie,
    csrf_for,
    flow_cookie_name,
    set_flow_cookie,
    set_session_cookie,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])

Provider = Literal["google", "github"]
LOGIN_PAGE = "/login"
SECURITY_PAGE = "/account/security"


# ---------------------------------------------------------------- response models
class CsrfResponse(BaseModel):
    csrf_token: str


class IdentityOut(BaseModel):
    id: UUID
    provider: str
    verified_email: str | None
    created_at: datetime


class MeResponse(BaseModel):
    id: UUID
    display_name: str
    primary_email: str | None
    identities: list[IdentityOut]
    operator: bool = False  # platform operations role (09); shows the operations view


class SessionOut(BaseModel):
    id: UUID
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    current: bool


class SessionList(BaseModel):
    items: list[SessionOut]
    next_cursor: None = None


class LinkResponse(BaseModel):
    redirect_url: str


# ---------------------------------------------------------------- helpers
def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _provider(request: Request, name: str) -> OAuthProvider | None:
    providers: dict[str, OAuthProvider] = request.app.state.oauth_providers
    return providers.get(name)


def _redirect(path: str, **query: str) -> RedirectResponse:
    if "error" in query:
        metrics.AUTH_FAILURES.labels(reason=metrics.label(query["error"])).inc()
    target = f"{path}?{urlencode(query)}" if query else path
    response = RedirectResponse(target, status_code=302)
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------- sign-in flow
@router.get("/auth/{provider}/start", response_class=RedirectResponse, status_code=302)
async def start(
    provider: Provider, request: Request, db: Db, return_to: str | None = Query(default=None, max_length=512)
) -> RedirectResponse:
    settings = _settings(request)
    try:
        await ratelimit.hit(
            request.app.state.rate_sessions, settings, "signin", ratelimit.client_address(request, settings)
        )
    except ApiError:
        return _redirect(LOGIN_PAGE, error="rate_limited")  # a browser navigation: never a JSON error
    impl = _provider(request, provider)
    if impl is None:
        return _redirect(LOGIN_PAGE, error="unavailable")
    settings = _settings(request)
    url, binding = await service.begin_authorization(
        db,
        settings,
        request.app.state.sealer,
        impl,
        purpose="login",
        return_path=safe_return_path(return_to),
    )
    response = _redirect(url)
    set_flow_cookie(response, settings, binding)  # login CSRF: the callback must come from this browser
    return response


@router.get("/auth/{provider}/callback", response_class=RedirectResponse, status_code=302)
async def callback(
    provider: Provider,
    request: Request,
    db: Db,
    principal: OptionalPrincipal,
    code: str | None = Query(default=None, max_length=2048),
    state: str | None = Query(default=None, max_length=256),
    error: str | None = Query(default=None, max_length=256),
) -> RedirectResponse:
    response = await _complete(provider, request, db, principal, code, state, error)
    clear_flow_cookie(response, _settings(request))  # a flow binding is single-use, whatever the outcome
    return response


async def _complete(
    provider: Provider,
    request: Request,
    db: Db,
    principal: OptionalPrincipal,
    code: str | None,
    state: str | None,
    error: str | None,
) -> RedirectResponse:
    impl = _provider(request, provider)
    if impl is None:
        return _redirect(LOGIN_PAGE, error="unavailable")
    if error or not code or not state:
        return _redirect(LOGIN_PAGE, error="denied" if error else "expired")
    settings = _settings(request)
    try:
        completed = await service.complete_authorization(
            db,
            settings,
            request.app.state.sealer,
            impl,
            state=state,
            code=code,
            binding=request.cookies.get(flow_cookie_name(settings)),
        )
    except AuthFlowError as flow:
        return _redirect(LOGIN_PAGE, error=flow.code)
    except OAuthError as failure:
        logger.warning("sign-in provider failure", extra={"provider": provider, "reason": failure.reason})
        return _redirect(LOGIN_PAGE, error="provider")

    if completed.purpose == "link":
        # The callback must arrive in the same session that started the link (09).
        if principal is None or principal.session_id != completed.initiating_session_id:
            return _redirect(LOGIN_PAGE, error="expired")
        try:
            await service.link_identity(db, principal, completed.identity)
        except AuthFlowError as flow:
            return _redirect(SECURITY_PAGE, error=flow.code)
        return _redirect(completed.return_path)

    try:
        token = await service.sign_in(db, settings, completed.identity, principal)
    except AuthFlowError as flow:
        return _redirect(LOGIN_PAGE, error=flow.code)
    response = _redirect(completed.return_path)
    set_session_cookie(response, settings, token)
    return response


# ---------------------------------------------------------------- session endpoints
@router.get("/auth/csrf", response_model=CsrfResponse)
async def csrf(request: Request, principal: CurrentPrincipal) -> CsrfResponse:
    return CsrfResponse(csrf_token=csrf_for(_settings(request), principal))


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, db: Db, principal: CsrfPrincipal) -> Response:
    await service.sign_out(db, principal)
    response = Response(status_code=204)
    clear_session_cookie(response, _settings(request))
    return response


@router.get("/me", response_model=MeResponse)
async def me(db: Db, principal: CurrentPrincipal) -> MeResponse:
    profile = await service.profile(db, principal)
    operator = (await db.execute(text("SELECT app_private.is_operator()"))).scalar_one()
    return MeResponse.model_validate({**profile, "operator": bool(operator)})


@router.get("/me/sessions", response_model=SessionList)
async def sessions(db: Db, principal: CurrentPrincipal) -> SessionList:
    return SessionList(items=[SessionOut(**s) for s in await service.list_sessions(db, principal)])


@router.delete("/me/sessions/{session_id}", status_code=204)
async def revoke_session(session_id: UUID, request: Request, db: Db, principal: CsrfPrincipal) -> Response:
    if not await service.revoke_session(db, principal, session_id):
        raise ApiError(404, "NOT_FOUND", "Session not found.")
    response = Response(status_code=204)
    if session_id == principal.session_id:
        clear_session_cookie(response, _settings(request))
    return response


# ---------------------------------------------------------------- linked identities
def _require_recent_sign_in(principal: CsrfPrincipal) -> None:
    if not principal.recently_authenticated():
        raise ApiError(403, "REAUTH_REQUIRED", "Sign in again to change sign-in methods.")


@router.post("/me/identities/{provider}/link", response_model=LinkResponse)
async def link(
    provider: Provider, request: Request, response: Response, db: Db, principal: CsrfPrincipal
) -> LinkResponse:
    _require_recent_sign_in(principal)
    impl = _provider(request, provider)
    if impl is None:
        raise ApiError(404, "NOT_FOUND", "Sign-in provider not available.")
    settings = _settings(request)
    url, binding = await service.begin_authorization(
        db,
        settings,
        request.app.state.sealer,
        impl,
        purpose="link",
        return_path=SECURITY_PAGE,
        initiating_session_id=principal.session_id,
    )
    set_flow_cookie(response, settings, binding)
    return LinkResponse(redirect_url=url)


@router.delete("/me/identities/{identity_id}", status_code=204)
async def unlink(identity_id: UUID, db: Db, principal: CsrfPrincipal) -> Response:
    _require_recent_sign_in(principal)
    outcome = await service.unlink_identity(db, principal, identity_id)
    if outcome == "not_found":
        raise ApiError(404, "NOT_FOUND", "Sign-in method not found.")
    if outcome == "last_identity":
        raise ApiError(409, "LAST_IDENTITY", "You cannot remove your only sign-in method.")
    return Response(status_code=204)
