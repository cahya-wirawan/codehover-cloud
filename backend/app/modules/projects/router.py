"""Project, build and private-viewer endpoints (06 "Endpoint inventory")."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from pydantic import BaseModel, Field, model_validator

from app.adapters.github.app import GitHubApp
from app.adapters.storage import ObjectStore
from app.core.errors import ApiError
from app.core.idempotency import run_once
from app.core.pagination import (
    Position,
    clamp_limit,
    decode_cursor,
    decode_path_cursor,
    encode_cursor,
    encode_path_cursor,
)
from app.core.settings import Settings
from app.engine.catalog import Catalog
from app.engine.estimate import GenerationConfig
from app.modules.identity.sessions import CsrfPrincipal, CurrentPrincipal, Db
from app.modules.projects import estimates, events, publication, quality, service
from app.modules.projects import exports as export_service
from app.modules.workspaces.access import Member, ReadMember, WriteMember, require_role, resource_member

router = APIRouter(tags=["projects"])


async def project_reader(project_id: UUID, db: Db, principal: CurrentPrincipal) -> Member:
    return await resource_member("project", project_id, db, principal)


async def project_writer(project_id: UUID, db: Db, principal: CsrfPrincipal) -> Member:
    return await resource_member("project", project_id, db, principal, write=True)


async def build_reader(build_id: UUID, db: Db, principal: CurrentPrincipal) -> Member:
    return await resource_member("build", build_id, db, principal)


async def build_writer(build_id: UUID, db: Db, principal: CsrfPrincipal) -> Member:
    return await resource_member("build", build_id, db, principal, write=True)


ProjectReader = Annotated[Member, Depends(project_reader)]
BuildWriter = Annotated[Member, Depends(build_writer)]
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
    """Either `estimate_id` (generation build, optional cap) or `ref` (M1 replay build, zero cap)."""

    ref: Annotated[str | None, Field(max_length=255)] = None
    estimate_id: UUID | None = None
    cap_micro_usd: Annotated[int | None, Field(ge=0, le=10**12)] = None

    @model_validator(mode="after")
    def _one_kind(self) -> BuildStart:
        if self.estimate_id is not None and self.ref is not None:
            raise ValueError("send either estimate_id or ref, not both")
        if self.estimate_id is None and self.cap_micro_usd is not None:
            raise ValueError("cap_micro_usd requires estimate_id")
        return self


class CancelIn(BaseModel):
    reason: Annotated[str | None, Field(max_length=200)] = None


class RetryIn(BaseModel):
    estimate_id: UUID | None = None
    cap_micro_usd: Annotated[int | None, Field(ge=0, le=10**12)] = None


class EventOut(BaseModel):
    sequence: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class EventList(BaseModel):
    items: list[EventOut]
    next_after: int


class SnapshotRequest(BaseModel):
    ref: Annotated[str | None, Field(max_length=255)] = None


class SnapshotOut(BaseModel):
    id: UUID
    project_id: UUID
    commit_sha: str
    observed_ref: str
    status: str
    rejection_code: str | None
    file_count: int | None
    source_bytes: int | None
    scan_summary: dict[str, int]
    created_at: datetime


class SnapshotFile(BaseModel):
    id: UUID
    path: str
    language: str | None
    status: str
    reason: str | None
    byte_count: int
    line_count: int | None


class SnapshotFileList(BaseModel):
    items: list[SnapshotFile]
    next_cursor: str | None


class EstimateRequest(BaseModel):
    snapshot_id: UUID
    config: GenerationConfig


class TokenRange(BaseModel):
    input: int
    output: int


class CostRange(BaseModel):
    low: int
    high: int


class EstimateOut(BaseModel):
    id: UUID
    project_id: UUID
    snapshot_id: UUID
    commit_sha: str
    config: dict[str, Any]
    config_digest: str
    price_version: str
    selected_files: int
    excluded_files: int
    estimated_tokens: dict[str, TokenRange]
    cost_micro_usd: CostRange
    created_at: datetime
    expires_at: datetime
    expired: bool
    disclaimer: str


class StageOut(BaseModel):
    stage: str
    state: str
    error_code: str | None


class BuildUsage(BaseModel):
    cap_micro_usd: int
    reserved_micro_usd: int
    settled_micro_usd: int


class BuildOut(BaseModel):
    id: UUID
    project_id: UUID
    snapshot_id: UUID
    estimate_id: UUID | None
    retry_of_id: UUID | None
    cancel_requested_at: datetime | None
    status: str
    commit_sha: str
    snapshot_status: str
    rejection_code: str | None
    engine_version: str
    prompt_version: str
    created_at: datetime
    completed_at: datetime | None
    content_purged_at: datetime | None = None  # retention removed the build's content (0016)
    stages: list[StageOut]
    counts: dict[str, int]
    usage: BuildUsage


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


class ChunkExplanation(BaseModel):
    start_line: int
    end_line: int
    incomplete: bool
    summary: str
    notes: list[dict[str, Any]]
    status: str


class FileExplanationOut(BaseModel):
    status: str
    summary: str
    chunks: list[ChunkExplanation]


class GuidePageSummary(BaseModel):
    slug: str
    title: str
    ordinal: int
    purpose: str
    status: str
    check_status: str
    provenance: dict[str, Any]


class GuideIndexOut(BaseModel):
    overview: dict[str, Any] | None
    pages: list[GuidePageSummary]


class Citation(BaseModel):
    file_id: str
    path: str | None
    start_line: int
    end_line: int


class ClaimCitation(BaseModel):
    citation: str
    path: str | None
    start_line: int
    end_line: int
    deterministic: str


class PageClaim(BaseModel):
    claim_key: str
    text: str
    ordinal: int
    status: str
    reason: str | None
    waived: bool
    citations: list[ClaimCitation]


class GuidePageOut(GuidePageSummary):
    markdown: str
    citations: list[Citation]
    claims: list[PageClaim]


class QualityOut(BaseModel):
    claims: int
    counts: dict[str, int]
    waived: dict[str, int]
    supported_fraction: float | None
    uncited_sentences: int
    pages: dict[str, int]
    publishable: bool
    blockers: list[str]
    requires_acknowledgment: bool


class CheckOut(BaseModel):
    id: UUID
    slug: str
    claim_key: str
    claim_text: str
    citation: str
    path: str | None
    start_line: int
    end_line: int
    deterministic: str
    status: str
    reason: str
    evidence_lines: list[int]
    checker_model: str | None
    waived: bool
    waived_reason: str | None


class CheckList(BaseModel):
    items: list[CheckOut]
    next_cursor: str | None


class WaiverIn(BaseModel):
    claim_keys: Annotated[
        list[Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]], Field(min_length=1, max_length=100)
    ]
    reason: Annotated[str, Field(min_length=10, max_length=1000)]


class WaiverOut(BaseModel):
    waived: list[str]


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
    explanation: FileExplanationOut | None = None


class Related(BaseModel):
    id: UUID
    name: str
    kind: str
    path: str
    file_id: UUID
    start_line: int
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


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _catalog(request: Request) -> Catalog:
    catalog: Catalog = request.app.state.catalog
    return catalog


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
        if body.estimate_id is not None:
            build = await service.start_generation_build(
                db,
                _catalog(request),
                _settings(request),
                member,
                project_id,
                body.estimate_id,
                body.cap_micro_usd,
            )
        else:
            build = await service.start_build(
                db, _github(request), _settings(request), member, project_id, body.ref
            )
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


# ---------------------------------------------------------------- snapshots and estimates
@router.post("/projects/{project_id}/snapshots", response_model=SnapshotOut, status_code=202)
async def request_snapshot(
    project_id: UUID,
    body: SnapshotRequest,
    request: Request,
    response: Response,
    db: Db,
    member: ProjectWriter,
) -> SnapshotOut:
    require_role(member, "editor")
    snapshot = await estimates.request_snapshot(
        db, _github(request), _settings(request), member, project_id, body.ref
    )
    response.headers["Location"] = f"/api/v1/projects/{project_id}/snapshots/{snapshot['id']}"
    return SnapshotOut(**snapshot)


@router.get("/projects/{project_id}/snapshots/{snapshot_id}", response_model=SnapshotOut)
async def get_snapshot(project_id: UUID, snapshot_id: UUID, db: Db, member: ProjectReader) -> SnapshotOut:
    return SnapshotOut(**await estimates.get_snapshot(db, member, project_id, snapshot_id))


@router.get("/projects/{project_id}/snapshots/{snapshot_id}/files", response_model=SnapshotFileList)
async def list_snapshot_files(
    project_id: UUID,
    snapshot_id: UUID,
    request: Request,
    db: Db,
    member: ProjectReader,
    status: Annotated[
        str | None, Query(pattern=r"^(selected|excluded|binary|oversize|unsupported|blocked)$")
    ] = None,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
) -> SnapshotFileList:
    scope = f"snapshot-files:{snapshot_id}:{status or ''}"
    secret = _settings(request).session_secret.get_secret_value()
    after = decode_path_cursor(secret, scope, cursor)
    n = clamp_limit(limit)
    rows = await estimates.list_snapshot_files(db, member, project_id, snapshot_id, status, after, n + 1)
    more = len(rows) > n
    rows = rows[:n]
    return SnapshotFileList(
        items=[SnapshotFile(**r) for r in rows],
        next_cursor=encode_path_cursor(secret, scope, rows[-1]["path"]) if more else None,
    )


@router.post("/projects/{project_id}/estimates", response_model=EstimateOut, status_code=201)
async def create_estimate(
    project_id: UUID,
    body: EstimateRequest,
    request: Request,
    response: Response,
    db: Db,
    member: ProjectWriter,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> EstimateOut:
    require_role(member, "editor")

    async def produce() -> tuple[int, dict[str, Any]]:
        created = await estimates.create_estimate(
            db, _catalog(request), _settings(request), member, project_id, body.snapshot_id, body.config
        )
        return 201, EstimateOut(**created).model_dump(mode="json")

    status, payload = await run_once(
        db,
        user_id=member.user_id,
        workspace_id=member.workspace_id,
        route=f"POST /projects/{project_id}/estimates",
        key=idempotency_key,
        body=body.model_dump(mode="json"),
        produce=produce,
    )
    response.status_code = status
    response.headers["Location"] = f"/api/v1/projects/{project_id}/estimates/{payload['id']}"
    return EstimateOut(**payload)


@router.get("/projects/{project_id}/estimates/{estimate_id}", response_model=EstimateOut)
async def get_estimate(project_id: UUID, estimate_id: UUID, db: Db, member: ProjectReader) -> EstimateOut:
    return EstimateOut(**await estimates.get_estimate(db, member, project_id, estimate_id))


# ---------------------------------------------------------------- guide
@router.get("/builds/{build_id}/guide", response_model=GuideIndexOut)
async def guide_index(build_id: UUID, request: Request, db: Db, member: BuildReader) -> GuideIndexOut:
    return GuideIndexOut(**await service.guide_index(db, _objects(request), member, build_id))


@router.get("/builds/{build_id}/guide/{slug}", response_model=GuidePageOut)
async def guide_page(
    build_id: UUID,
    request: Request,
    db: Db,
    member: BuildReader,
    slug: Annotated[str, Path(pattern=r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")],
) -> GuidePageOut:
    return GuidePageOut(**await service.guide_page(db, _objects(request), member, build_id, slug))


# ---------------------------------------------------------------- quality
@router.get("/builds/{build_id}/quality", response_model=QualityOut)
async def quality_summary(build_id: UUID, request: Request, db: Db, member: BuildReader) -> QualityOut:
    return QualityOut(**await quality.summary(db, _objects(request), member, build_id))


@router.get("/builds/{build_id}/quality/checks", response_model=CheckList)
async def quality_checks(
    build_id: UUID,
    request: Request,
    db: Db,
    member: BuildReader,
    status: Annotated[
        str | None, Query(pattern=r"^(supported|partial|unsupported|unchecked|invalid)$")
    ] = None,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
) -> CheckList:
    secret = _settings(request).session_secret.get_secret_value()
    scope = f"quality:{build_id}:{status or ''}"
    n = clamp_limit(limit)
    rows = await quality.checks(db, member, build_id, status, decode_cursor(secret, scope, cursor), n + 1)
    more = len(rows) > n
    rows = rows[:n]
    return CheckList(
        items=[CheckOut(**r) for r in rows],
        next_cursor=encode_cursor(secret, scope, Position(rows[-1]["created_at"], rows[-1]["id"]))
        if more
        else None,
    )


@router.post("/builds/{build_id}/quality/waivers", response_model=WaiverOut)
async def waive_claims(build_id: UUID, body: WaiverIn, db: Db, member: BuildWriter) -> WaiverOut:
    require_role(member, "admin")
    return WaiverOut(
        **await quality.waive(db, member, build_id, sorted(set(body.claim_keys)), body.reason.strip())
    )


# ---------------------------------------------------------------- cancel, retry, events
@router.post("/builds/{build_id}/cancel", response_model=BuildOut)
async def cancel_build(build_id: UUID, body: CancelIn, db: Db, member: BuildWriter) -> BuildOut:
    require_role(member, "editor")
    return BuildOut(**await service.request_cancel(db, member, build_id, body.reason))


@router.post("/builds/{build_id}/retry", response_model=BuildOut, status_code=202)
async def retry_build(
    build_id: UUID,
    body: RetryIn,
    request: Request,
    response: Response,
    db: Db,
    member: BuildWriter,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> BuildOut:
    require_role(member, "editor")

    async def produce() -> tuple[int, dict[str, Any]]:
        build = await service.retry_build(
            db,
            _github(request),
            _catalog(request),
            _settings(request),
            member,
            build_id,
            body.estimate_id,
            body.cap_micro_usd,
        )
        return 202, BuildOut(**build).model_dump(mode="json")

    status, payload = await run_once(
        db,
        user_id=member.user_id,
        workspace_id=member.workspace_id,
        route=f"POST /builds/{build_id}/retry",
        key=idempotency_key,
        body=body.model_dump(mode="json"),
        produce=produce,
    )
    response.status_code = status
    response.headers["Location"] = f"/api/v1/builds/{payload['id']}"
    return BuildOut(**payload)


@router.get(
    "/builds/{build_id}/events",
    response_model=EventList,
    responses={200: {"content": {"text/event-stream": {}}, "description": "JSON page, or an SSE stream"}},
)
async def build_events(
    build_id: UUID,
    request: Request,
    db: Db,
    member: BuildReader,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    last_event_id: Annotated[str | None, Header(max_length=20)] = None,
) -> Any:
    """`Accept: text/event-stream` streams events (resume with Last-Event-ID); otherwise a JSON page."""
    if "text/event-stream" not in request.headers.get("accept", ""):
        return EventList(**await service.build_events(db, member, build_id, after, limit))
    await service.build_events(db, member, build_id, 0, 1)  # 404 for unknown builds before streaming
    start = int(last_event_id) if last_event_id and last_event_id.isdigit() else after
    slot = events.StreamSlot(member.principal.session_id, member.workspace_id).__enter__()
    try:
        await db.commit()
    except BaseException:
        slot.__exit__()
        raise

    async def body() -> AsyncIterator[str]:
        try:
            async for chunk in events.stream(
                request.app.state.sessions,
                token=member.principal.session_token,
                user_id=member.user_id,
                workspace_id=member.workspace_id,
                build_id=build_id,
                after=start,
                timing=getattr(request.app.state, "event_timing", events.Timing()),
                disconnected=request.is_disconnected,
            ):
                yield chunk
        finally:
            slot.__exit__()

    return events.SlotResponse(
        body(),
        slot,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------- search
class SearchHit(BaseModel):
    kind: str
    ref_id: UUID
    name: str
    path: str | None
    line: int | None
    symbol_kind: str | None
    snippet: str
    basis: str
    score: float


class SearchOut(BaseModel):
    items: list[SearchHit]
    next_cursor: None = None


@router.get("/builds/{build_id}/search", response_model=SearchOut)
async def search_build(
    build_id: UUID,
    db: Db,
    member: BuildReader,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    kind: Annotated[str | None, Query(pattern=r"^(symbol|file|guide)$")] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> SearchOut:
    return SearchOut(
        items=[SearchHit(**h) for h in await service.search_build(db, member, build_id, q, kind, limit)]
    )


# ---------------------------------------------------------------- publication
class PublicationIn(BaseModel):
    build_id: UUID
    visibility: Literal["private", "public"] = "private"
    public_slug: Annotated[str | None, Field(max_length=63)] = None
    confirmation: Annotated[str | None, Field(max_length=200)] = None
    acknowledge_partial: bool = False


class PublicationOut(BaseModel):
    build_id: UUID
    visibility: str
    public_slug: str | None
    version: int
    acknowledged_partial: bool
    suspended_at: datetime | None
    suspended_reason: str | None
    updated_at: datetime
    commit_sha: str


@router.get("/projects/{project_id}/publication", response_model=PublicationOut)
async def get_publication(
    project_id: UUID, response: Response, db: Db, member: ProjectReader
) -> PublicationOut:
    found = await publication.current(db, member, project_id)
    if found is None:
        raise ApiError(404, "NOT_PUBLISHED", "This project is not published.")
    response.headers["ETag"] = publication.etag(found["version"])
    return PublicationOut(**found)


@router.post("/projects/{project_id}/publication", response_model=PublicationOut)
async def publish(
    project_id: UUID,
    body: PublicationIn,
    request: Request,
    response: Response,
    db: Db,
    member: ProjectWriter,
    idempotency_key: Annotated[str | None, Header()] = None,
    if_match: Annotated[str | None, Header(max_length=100)] = None,
) -> PublicationOut:
    require_role(member, "admin")

    async def produce() -> tuple[int, dict[str, Any]]:
        result = await publication.publish(
            db,
            _objects(request),
            member,
            project_id,
            build_id=body.build_id,
            visibility=body.visibility,
            public_slug=body.public_slug,
            confirmation=body.confirmation,
            acknowledge_partial=body.acknowledge_partial,
            if_match=if_match,
        )
        return 200, PublicationOut(**result).model_dump(mode="json")

    status, payload = await run_once(
        db,
        user_id=member.user_id,
        workspace_id=member.workspace_id,
        route=f"POST /projects/{project_id}/publication",
        key=idempotency_key,
        body={**body.model_dump(mode="json"), "if_match": if_match},
        produce=produce,
    )
    response.status_code = status
    response.headers["ETag"] = publication.etag(payload["version"])
    return PublicationOut(**payload)


@router.delete("/projects/{project_id}/publication", status_code=204)
async def unpublish(
    project_id: UUID,
    db: Db,
    member: ProjectWriter,
    if_match: Annotated[str | None, Header(max_length=100)] = None,
) -> Response:
    require_role(member, "admin")
    await publication.unpublish(db, member, project_id, if_match)
    return Response(status_code=204)


# ---------------------------------------------------------------- exports
class ExportIn(BaseModel):
    format: Literal["static_zip", "kb_json"]


class ExportOut(BaseModel):
    id: UUID
    build_id: UUID
    format: str
    status: str
    byte_count: int | None
    error_code: str | None
    created_at: datetime
    completed_at: datetime | None
    expires_at: datetime | None
    expired: bool


async def export_reader(export_id: UUID, db: Db, principal: CurrentPrincipal) -> Member:
    return await resource_member("export", export_id, db, principal)


ExportReader = Annotated[Member, Depends(export_reader)]


@router.post("/builds/{build_id}/exports", response_model=ExportOut, status_code=202)
async def request_export(
    build_id: UUID,
    body: ExportIn,
    response: Response,
    db: Db,
    member: BuildWriter,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> ExportOut:
    require_role(member, "editor")

    async def produce() -> tuple[int, dict[str, Any]]:
        created = await export_service.request_export(db, member, build_id, body.format)
        return 202, ExportOut(**created).model_dump(mode="json")

    status, payload = await run_once(
        db,
        user_id=member.user_id,
        workspace_id=member.workspace_id,
        route=f"POST /builds/{build_id}/exports",
        key=idempotency_key,
        body=body.model_dump(mode="json"),
        produce=produce,
    )
    response.status_code = status
    response.headers["Location"] = f"/api/v1/exports/{payload['id']}"
    return ExportOut(**payload)


@router.get("/exports/{export_id}", response_model=ExportOut)
async def get_export(export_id: UUID, db: Db, member: ExportReader) -> ExportOut:
    return ExportOut(**await export_service.get_export(db, member, export_id))


@router.get("/exports/{export_id}/download", response_class=Response)
async def download_export(export_id: UUID, request: Request, db: Db, member: ExportReader) -> Response:
    key, content_type, name, _row = await export_service.download(db, member, export_id)
    body = await _objects(request).get(key)  # no transaction is open while reading object storage
    return Response(
        content=body,
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ---------------------------------------------------------------- freshness
class FreshnessOut(BaseModel):
    tracked_ref: str
    state: Literal["current", "update_available", "never_built", "unknown"]
    reason: str | None
    latest_sha: str | None
    last_observed_sha: str | None
    last_observed_at: datetime | None
    published_sha: str | None
    latest_ready_sha: str | None


@router.get("/projects/{project_id}/freshness", response_model=FreshnessOut)
async def project_freshness(
    project_id: UUID, request: Request, db: Db, member: ProjectReader
) -> FreshnessOut:
    return FreshnessOut(**await service.freshness(db, _github(request), member, project_id))
