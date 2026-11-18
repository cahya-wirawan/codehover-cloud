"""Published site delivery: the same reading experience for a published build (06, 02 S20, 09).

Every request resolves the slug again through `app_private.resolve_publication`, which returns only
unsuspended publications, so unpublishing or a revoked repository stops delivery on the next request
(no cache, `no-store`). A public publication is readable by anyone; a private one only by signed-in
members of its workspace; everything else is 404, so slugs and unpublished builds cannot be probed.
The workspace context comes from the resolver, never from request data, and the build ID is never
returned. Responses are JSON rendered by the product's viewer, never tenant HTML.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel
from sqlalchemy import text

from app.adapters.storage import ObjectStore
from app.core.errors import ApiError
from app.core.tenancy import set_context
from app.modules.identity.sessions import Db, OptionalPrincipal
from app.modules.projects import service
from app.modules.projects.router import (
    FileOut,
    GuideIndexOut,
    GuidePageOut,
    SearchHit,
    SearchOut,
    SymbolOut,
    TreeEntry,
    TreeOut,
)
from app.modules.workspaces.access import PublicReader

router = APIRouter(tags=["published"])
SlugPath = Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9-]{2,62}$")]


@dataclass(frozen=True)
class Site:
    reader: PublicReader
    project_id: UUID
    build_id: UUID
    visibility: str


async def resolve_site(slug: SlugPath, db: Db, principal: OptionalPrincipal) -> Site:
    found = (
        await db.execute(
            text(
                "SELECT workspace_id, project_id, build_id, visibility FROM app_private.resolve_publication(:s)"
            ),
            {"s": slug},
        )
    ).one_or_none()
    if found is None:
        raise ApiError(404, "NOT_FOUND", "Site not found.")
    if found.visibility != "public":
        member = None
        if principal is not None:
            member = (
                await db.execute(
                    text("SELECT 1 FROM memberships WHERE workspace_id = :w AND user_id = :u"),
                    {"w": found.workspace_id, "u": principal.user_id},
                )
            ).one_or_none()
        if member is None:
            raise ApiError(404, "NOT_FOUND", "Site not found.")
    await set_context(db, workspace_id=found.workspace_id)
    return Site(PublicReader(found.workspace_id), found.project_id, found.build_id, found.visibility)


SiteDep = Annotated[Site, Depends(resolve_site)]


class SiteOut(BaseModel):
    project: str
    repository: str
    commit_sha: str
    visibility: str
    generated: bool
    published_at: Any


def _objects(request: Request) -> ObjectStore:
    store: ObjectStore = request.app.state.objects
    return store


@router.get("/published/{slug}", response_model=SiteOut)
async def site(slug: SlugPath, db: Db, found: SiteDep) -> SiteOut:
    row = (
        await db.execute(
            text(
                "SELECT p.name AS project, r.owner || '/' || r.name AS repository, s.commit_sha, b.estimate_id,"
                " pub.updated_at FROM publications pub"
                " JOIN projects p ON p.workspace_id = pub.workspace_id AND p.id = pub.project_id"
                " JOIN repositories r ON r.workspace_id = p.workspace_id AND r.id = p.repository_id"
                " JOIN builds b ON b.workspace_id = pub.workspace_id AND b.id = pub.build_id"
                " JOIN snapshots s ON s.workspace_id = b.workspace_id AND s.id = b.snapshot_id"
                " WHERE pub.workspace_id = :w AND pub.project_id = :p"
            ),
            {"w": found.reader.workspace_id, "p": found.project_id},
        )
    ).one()
    return SiteOut(
        project=row.project,
        repository=row.repository,
        commit_sha=row.commit_sha,
        visibility=found.visibility,
        generated=row.estimate_id is not None,
        published_at=row.updated_at,
    )


@router.get("/published/{slug}/tree", response_model=TreeOut)
async def tree(
    slug: SlugPath, db: Db, found: SiteDep, path: Annotated[str, Query(max_length=1024)] = ""
) -> TreeOut:
    return TreeOut(items=[TreeEntry(**e) for e in await service.tree(db, found.reader, found.build_id, path)])


@router.get("/published/{slug}/files/{file_id}", response_model=FileOut)
async def file_view(
    slug: SlugPath,
    file_id: UUID,
    request: Request,
    db: Db,
    found: SiteDep,
    start_line: Annotated[int, Query(ge=1)] = 1,
    end_line: Annotated[int | None, Query(ge=1)] = None,
) -> FileOut:
    return FileOut(
        **await service.file_view(
            db, _objects(request), found.reader, found.build_id, file_id, start_line, end_line
        )
    )


@router.get("/published/{slug}/symbols/{symbol_id}", response_model=SymbolOut)
async def symbol_view(slug: SlugPath, symbol_id: UUID, db: Db, found: SiteDep) -> SymbolOut:
    return SymbolOut(**await service.symbol_view(db, found.reader, found.build_id, symbol_id))


@router.get("/published/{slug}/guide", response_model=GuideIndexOut)
async def guide_index(slug: SlugPath, request: Request, db: Db, found: SiteDep) -> GuideIndexOut:
    return GuideIndexOut(**await service.guide_index(db, _objects(request), found.reader, found.build_id))


@router.get("/published/{slug}/guide/{page}", response_model=GuidePageOut)
async def guide_page(
    slug: SlugPath,
    request: Request,
    db: Db,
    found: SiteDep,
    page: Annotated[str, Path(pattern=r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")],
) -> GuidePageOut:
    return GuidePageOut(**await service.guide_page(db, _objects(request), found.reader, found.build_id, page))


@router.get("/published/{slug}/search", response_model=SearchOut)
async def search(
    slug: SlugPath,
    db: Db,
    found: SiteDep,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    kind: Annotated[str | None, Query(pattern=r"^(symbol|file|guide)$")] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> SearchOut:
    hits = await service.search_build(db, found.reader, found.build_id, q, kind, limit)
    return SearchOut(items=[SearchHit(**h) for h in hits])
