"""Project, build and private-viewer endpoints (06 "Endpoint inventory")."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, Field

from app.adapters.github.app import GitHubApp
from app.adapters.storage import ObjectStore
from app.core.idempotency import run_once
from app.modules.identity.sessions import CsrfPrincipal, CurrentPrincipal, Db
from app.modules.projects import service
from app.modules.workspaces.access import Member, ReadMember, WriteMember, require_role, resource_member

router = APIRouter(tags=["projects"])


async def project_reader(project_id: UUID, db: Db, principal: CurrentPrincipal) -> Member:
    return await resource_member("project", project_id, db, principal)


async def project_writer(project_id: UUID, db: Db, principal: CsrfPrincipal) -> Member:
    return await resource_member("project", project_id, db, principal)


async def build_reader(build_id: UUID, db: Db, principal: CurrentPrincipal) -> Member:
    return await resource_member("build", build_id, db, principal)


ProjectReader = Annotated[Member, Depends(project_reader)]
ProjectWriter = Annotated[Member, Depends(project_writer)]
BuildReader = Annotated[Member, Depends(build_reader)]


class ProjectCreate(BaseModel):
    repository_id: UUID
    name: Annotated[str, Field(min_length=1, max_length=80)]
    slug: Annotated[str, Field(pattern=r"^[a-z0-9]([a-z0-9-]{0,46}[a-z0-9])?$")]
    tracked_ref: Annotated[str, Field(max_length=255)] = ""


class ProjectOut(BaseModel):
    id: UUID
    workspace_id: UUID
    name: str
    slug: str
    tracked_ref: str
    owner: str
    repository: str


class ProjectList(BaseModel):
    items: list[ProjectOut]
    next_cursor: None = None


class BuildStart(BaseModel):
    ref: Annotated[str | None, Field(max_length=255)] = None


class StageOut(BaseModel):
    stage: str
    state: str
    error_code: str | None


class BuildOut(BaseModel):
    id: UUID
    project_id: UUID
    status: str
    commit_sha: str
    snapshot_status: str
    rejection_code: str | None
    engine_version: str
    prompt_version: str
    created_at: datetime
    completed_at: datetime | None
    stages: list[StageOut]
    counts: dict[str, int]


class BuildSummary(BaseModel):
    id: UUID
    status: str
    commit_sha: str
    created_at: datetime
    completed_at: datetime | None


class BuildList(BaseModel):
    items: list[BuildSummary]
    next_cursor: None = None


class TreeEntry(BaseModel):
    name: str
    path: str
    type: str
    file_id: UUID | None = None
    language: str | None = None
    status: str | None = None
    reason: str | None = None


class TreeOut(BaseModel):
    items: list[TreeEntry]
    next_cursor: None = None


class FileSymbol(BaseModel):
    id: UUID
    symbol_key: str
    name: str
    kind: str
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    summary: str | None


class FileOut(BaseModel):
    id: UUID
    path: str
    language: str | None
    sha256: str | None
    line_count: int | None
    start_line: int
    end_line: int
    lines: list[str]
    symbols: list[FileSymbol]


class Related(BaseModel):
    id: UUID
    name: str
    kind: str
    path: str
    resolution: str
    ambiguous: bool


class SymbolOut(BaseModel):
    id: UUID
    symbol_key: str
    name: str
    kind: str
    signature: str | None
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    card: dict[str, Any]
    file_id: UUID
    path: str
    uses: list[Related]
    used_by: list[Related]


def _github(request: Request) -> GitHubApp | None:
    app: GitHubApp | None = request.app.state.github_app
    return app


def _objects(request: Request) -> ObjectStore:
    store: ObjectStore = request.app.state.objects
    return store


# ---------------------------------------------------------------- projects
@router.get("/workspaces/{workspace_id}/projects", response_model=ProjectList)
async def list_projects(db: Db, member: ReadMember) -> ProjectList:
    return ProjectList(items=[ProjectOut(**p) for p in await service.list_projects(db, member)])


@router.post("/workspaces/{workspace_id}/projects", response_model=ProjectOut, status_code=201)
async def create_project(body: ProjectCreate, db: Db, member: WriteMember) -> ProjectOut:
    require_role(member, "editor")
    return ProjectOut(
        **await service.create_project(
            db, member, body.repository_id, body.name.strip(), body.slug, body.tracked_ref.strip()
        )
    )


@router.get("/projects/{project_id}", response_model=ProjectOut)
async def get_project(project_id: UUID, db: Db, member: ProjectReader) -> ProjectOut:
    return ProjectOut(**await service.get_project(db, member, project_id))


# ---------------------------------------------------------------- builds
@router.get("/projects/{project_id}/builds", response_model=BuildList)
async def list_builds(project_id: UUID, db: Db, member: ProjectReader) -> BuildList:
    return BuildList(items=[BuildSummary(**b) for b in await service.list_builds(db, member, project_id)])


@router.post("/projects/{project_id}/builds", response_model=BuildOut, status_code=202)
async def start_build(
    project_id: UUID,
    body: BuildStart,
    request: Request,
    response: Response,
    db: Db,
    member: ProjectWriter,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> BuildOut:
    require_role(member, "editor")

    async def produce() -> tuple[int, dict[str, Any]]:
        build = await service.start_build(db, _github(request), member, project_id, body.ref)
        return 202, BuildOut(**build).model_dump(mode="json")

    status, payload = await run_once(
        db,
        user_id=member.user_id,
        workspace_id=member.workspace_id,
        route=f"POST /projects/{project_id}/builds",
        key=idempotency_key,
        body=body.model_dump(mode="json"),
        produce=produce,
    )
    response.status_code = status
    response.headers["Location"] = f"/api/v1/builds/{payload['id']}"
    return BuildOut(**payload)


@router.get("/builds/{build_id}", response_model=BuildOut)
async def get_build(build_id: UUID, db: Db, member: BuildReader) -> BuildOut:
    return BuildOut(**await service.get_build(db, member, build_id))


# ---------------------------------------------------------------- viewer
@router.get("/builds/{build_id}/tree", response_model=TreeOut)
async def tree(
    build_id: UUID, db: Db, member: BuildReader, path: Annotated[str, Query(max_length=1024)] = ""
) -> TreeOut:
    return TreeOut(items=[TreeEntry(**e) for e in await service.tree(db, member, build_id, path)])


@router.get("/builds/{build_id}/files/{file_id}", response_model=FileOut)
async def file_view(
    build_id: UUID,
    file_id: UUID,
    request: Request,
    db: Db,
    member: BuildReader,
    start_line: Annotated[int, Query(ge=1)] = 1,
    end_line: Annotated[int | None, Query(ge=1)] = None,
) -> FileOut:
    return FileOut(
        **await service.file_view(db, _objects(request), member, build_id, file_id, start_line, end_line)
    )


@router.get("/builds/{build_id}/symbols/{symbol_id}", response_model=SymbolOut)
async def symbol_view(build_id: UUID, symbol_id: UUID, db: Db, member: BuildReader) -> SymbolOut:
    return SymbolOut(**await service.symbol_view(db, member, build_id, symbol_id))
