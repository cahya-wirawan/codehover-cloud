"""GitHub connection, repository and ref endpoints (06 "Endpoint inventory")."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.adapters.github.app import GitHubApp
from app.core.errors import ApiError
from app.core.settings import Settings
from app.modules.github import service
from app.modules.github.service import ConnectError
from app.modules.identity.sessions import (
    Db,
    OptionalPrincipal,
    clear_flow_cookie,
    flow_cookie_name,
    set_flow_cookie,
)
from app.modules.workspaces.access import ReadMember, WriteMember, require_role

logger = logging.getLogger(__name__)
router = APIRouter(tags=["github"])


class InstallStart(BaseModel):
    install_url: str


class ConnectionOut(BaseModel):
    id: UUID
    installation_id: int
    account: str
    status: Literal["active", "suspended", "revoked"]
    last_verified_at: datetime
    created_at: datetime


class ConnectionList(BaseModel):
    items: list[ConnectionOut]
    next_cursor: None = None


class RepositoryOut(BaseModel):
    id: UUID
    provider_repo_id: int
    owner: str
    name: str
    visibility: str
    default_branch: str


class RepositoryList(BaseModel):
    items: list[RepositoryOut]
    next_cursor: None = None


class RefOut(BaseModel):
    name: str
    kind: Literal["branch", "tag"]
    sha: str


class RefList(BaseModel):
    items: list[RefOut]
    next_cursor: None = None


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _app(request: Request) -> GitHubApp | None:
    app: GitHubApp | None = request.app.state.github_app
    return app


def _require_app(request: Request) -> GitHubApp:
    app = _app(request)
    if app is None:
        raise ApiError(503, "GITHUB_APP_UNAVAILABLE", "GitHub repository access is not configured.")
    return app


def _redirect(target: str, **query: str) -> RedirectResponse:
    response = RedirectResponse(f"{target}?{urlencode(query)}" if query else target, status_code=302)
    response.headers["Cache-Control"] = "no-store"
    return response


def _connections_page(workspace_id: UUID | None, **query: str) -> RedirectResponse:
    if workspace_id is None:
        return _redirect("/login", error=query.get("error", "expired"))
    return _redirect(f"/w/{workspace_id}/connections", **query)


# ---------------------------------------------------------------- connect flow
@router.post("/workspaces/{workspace_id}/github/install", response_model=InstallStart)
async def start_install(request: Request, response: Response, db: Db, member: WriteMember) -> InstallStart:
    require_role(member, "admin")
    url, binding = await service.begin_install(db, request.app.state.sealer, _require_app(request), member)
    set_flow_cookie(response, _settings(request), binding)
    return InstallStart(install_url=url)


@router.get("/integrations/github/setup", response_class=RedirectResponse, status_code=302)
async def setup(
    request: Request,
    db: Db,
    principal: OptionalPrincipal,
    installation_id: Annotated[int | None, Query(ge=1)] = None,
    state: Annotated[str | None, Query(max_length=256)] = None,
) -> RedirectResponse:
    settings = _settings(request)
    app = _app(request)
    if app is None or installation_id is None or not state:
        response = _connections_page(None, error="expired")
    else:
        try:
            url, binding, _ = await service.continue_after_install(
                db,
                settings,
                request.app.state.sealer,
                app,
                principal,
                state=state,
                binding=request.cookies.get(flow_cookie_name(settings)),
                installation_id=installation_id,
            )
        except ConnectError as error:
            response = _connections_page(error.workspace_id, error=error.code)
        else:
            response = _redirect(url)
            set_flow_cookie(response, settings, binding)  # binds the authorization phase to this browser
            return response
    clear_flow_cookie(response, settings)
    return response


@router.get("/integrations/github/callback", response_class=RedirectResponse, status_code=302)
async def callback(
    request: Request,
    db: Db,
    principal: OptionalPrincipal,
    code: Annotated[str | None, Query(max_length=2048)] = None,
    state: Annotated[str | None, Query(max_length=256)] = None,
    error: Annotated[str | None, Query(max_length=256)] = None,
) -> RedirectResponse:
    settings = _settings(request)
    app = _app(request)
    if app is None or error or not code or not state:
        response = _connections_page(None, error="denied" if error else "expired")
    else:
        try:
            workspace_id = await service.finish_connection(
                db,
                settings,
                request.app.state.sealer,
                app,
                principal,
                state=state,
                binding=request.cookies.get(flow_cookie_name(settings)),
                code=code,
            )
        except ConnectError as failure:
            logger.info("github connect refused", extra={"reason": failure.code})
            response = _connections_page(failure.workspace_id, error=failure.code)
        else:
            response = _connections_page(workspace_id, connected="1")
    clear_flow_cookie(response, settings)
    return response


# ---------------------------------------------------------------- connections and repositories
@router.get("/workspaces/{workspace_id}/connections", response_model=ConnectionList)
async def list_connections(db: Db, member: ReadMember) -> ConnectionList:
    require_role(member, "admin")
    return ConnectionList(items=[ConnectionOut(**c) for c in await service.list_connections(db, member)])


@router.delete("/workspaces/{workspace_id}/connections/{connection_id}", status_code=204)
async def revoke_connection(connection_id: UUID, db: Db, member: WriteMember) -> Response:
    require_role(member, "admin")
    await service.revoke_connection(db, member, connection_id)
    return Response(status_code=204)


@router.get("/workspaces/{workspace_id}/repositories", response_model=RepositoryList)
async def list_repositories(
    request: Request, db: Db, member: ReadMember, connection_id: Annotated[UUID, Query()]
) -> RepositoryList:
    require_role(member, "editor")
    repos = await service.list_repositories(db, _require_app(request), member, connection_id)
    return RepositoryList(items=[RepositoryOut(**r) for r in repos])


@router.get("/workspaces/{workspace_id}/repositories/{repository_id}/refs", response_model=RefList)
async def list_refs(repository_id: UUID, request: Request, db: Db, member: ReadMember) -> RefList:
    require_role(member, "editor")
    refs = await service.list_refs(db, _require_app(request), member, repository_id)
    return RefList(items=[RefOut(**r) for r in refs])
