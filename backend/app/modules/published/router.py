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
from app.core import ratelimit
from app.core.errors import ApiError
from app.core.tenancy import set_context
from app.engine.prompts import LANGUAGE_PATTERN
from app.modules.identity.sessions import Db, OptionalPrincipal
from app.modules.projects import quality, service
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
# 0039: a site lives at its owner's handle and the project's slug (/sites/<owner>/<project>).
OWNER = r"^[a-z0-9]+(-[a-z0-9]+)*$"
PROJECT = r"^[a-z0-9]([a-z0-9-]{0,46}[a-z0-9])?$"
OwnerPath = Annotated[str, Path(pattern=OWNER, max_length=39)]
ProjectPath = Annotated[str, Path(pattern=PROJECT)]
AliasPath = Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9-]{2,62}$")]  # a pre-0039 one-segment address


@dataclass(frozen=True)
class Site:
    reader: PublicReader
    project_id: UUID
    build_id: UUID
    visibility: str
    address: str = ""  # the canonical "<owner>/<project>" (the owner may have been renamed since a link)


async def find_site(db: Any, owner: str, project: str) -> Any:
    """The unsuspended publication at an address, or None (app_private.resolve_site)."""
    return (
        await db.execute(
            text(
                "SELECT workspace_id, project_id, build_id, visibility, owner, project"
                " FROM app_private.resolve_site(:o, :p)"
            ),
            {"o": owner, "p": project},
        )
    ).one_or_none()


async def resolve_site(
    owner: OwnerPath, project: ProjectPath, request: Request, db: Db, principal: OptionalPrincipal
) -> Site:
    settings = request.app.state.settings
    await ratelimit.hit(
        request.app.state.rate_sessions, settings, "public", ratelimit.client_address(request, settings)
    )
    found = await find_site(db, owner, project)
    if found is None:
        raise ApiError(404, "NOT_FOUND", "Site not found.")
    if found.visibility != "public":
        member = None
        if principal is not None:  # a member who may see this project (0027: restricted projects too)
            member = (
                await db.execute(
                    text(
                        "SELECT 1 FROM workspaces w WHERE w.id = :w AND w.status = 'active'"
                        " AND app_private.project_role(:w, :p, :u) IS NOT NULL"
                    ),
                    {"w": found.workspace_id, "p": found.project_id, "u": principal.user_id},
                )
            ).one_or_none()
        if member is None:
            raise ApiError(404, "NOT_FOUND", "Site not found.")
    await set_context(db, workspace_id=found.workspace_id)
    return Site(
        PublicReader(found.workspace_id),
        found.project_id,
        found.build_id,
        found.visibility,
        f"{found.owner}/{found.project}",
    )


SiteDep = Annotated[Site, Depends(resolve_site)]
LanguageQuery = Annotated[str | None, Query(pattern=LANGUAGE_PATTERN)]


async def site_variants(db: Any, found: Site) -> list[dict[str, Any]]:
    """The published build and the language variants its publish approved (FR-026, recorded on the
    publication), while each is still ready, keeps its content and passes the review gate."""
    rows = list(
        await db.execute(
            text(
                "SELECT b.id, b.created_at, COALESCE(b.config->'generation'->'languages'->>0, 'en') AS language,"
                " b.id = pub.build_id AS published FROM publications pub JOIN builds b"
                " ON b.workspace_id = pub.workspace_id AND (b.id = pub.build_id OR b.id = ANY(pub.variant_build_ids))"
                " WHERE pub.workspace_id = :w AND pub.project_id = :p AND pub.build_id = :b"
                " AND b.status = 'ready' AND b.content_purged_at IS NULL"
            ),
            {"w": found.reader.workspace_id, "p": found.project_id, "b": found.build_id},
        )
    )
    served: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda r: (not r.published, r.created_at)):
        if row.language in served:
            continue
        if not row.published:
            blockers, _partial = await quality.gate(db, found.reader.workspace_id, row.id)
            if blockers:  # partly supported claims were acknowledged by the publish that approved it
                continue
        served[row.language] = {"id": row.id, "language": row.language, "published": row.published}
    return list(served.values())


async def site_build(db: Db, found: SiteDep, language: LanguageQuery = None) -> UUID:
    """The build a site request reads: the published one, or its variant in `language` when the site
    serves one (404 otherwise, like any page the site does not have)."""
    if language is None:
        return found.build_id
    for variant in await site_variants(db, found):
        if variant["language"] == language:
            return UUID(str(variant["id"]))
    raise ApiError(404, "NOT_FOUND", "This site has no such language.")


SiteBuild = Annotated[UUID, Depends(site_build)]


class SiteVariant(BaseModel):
    language: str
    published: bool  # the published build itself; the others are its language variants


class SiteVariantsOut(BaseModel):
    items: list[SiteVariant]


class SiteLicense(BaseModel):
    spdx_id: str
    path: str
    file_id: UUID | None  # the license file in the site's code, when the build still has it


class SiteOut(BaseModel):
    address: str  # canonical "<owner>/<project>"; differs from the request after an owner was renamed
    project: str
    repository: str
    commit_sha: str
    visibility: str
    generated: bool
    published_at: Any
    license: SiteLicense | None = None  # FR-029: named on public sites of public-source repositories


def _objects(request: Request) -> ObjectStore:
    store: ObjectStore = request.app.state.objects
    return store


@router.get("/published/{owner}/{project}", response_model=SiteOut)
async def site(owner: OwnerPath, project: ProjectPath, db: Db, found: SiteDep) -> SiteOut:
    row = (
        await db.execute(
            text(
                "SELECT p.name AS project, r.owner || '/' || r.name AS repository, s.commit_sha, b.estimate_id,"
                " pub.updated_at, pub.license_spdx, pub.license_path, f.id AS license_file FROM publications pub"
                " JOIN projects p ON p.workspace_id = pub.workspace_id AND p.id = pub.project_id"
                " JOIN repositories r ON r.workspace_id = p.workspace_id AND r.id = p.repository_id"
                " JOIN builds b ON b.workspace_id = pub.workspace_id AND b.id = pub.build_id"
                " JOIN snapshots s ON s.workspace_id = b.workspace_id AND s.id = b.snapshot_id"
                " LEFT JOIN source_files f ON f.workspace_id = s.workspace_id AND f.snapshot_id = s.id"
                " AND f.path = pub.license_path"
                " WHERE pub.workspace_id = :w AND pub.project_id = :p"
            ),
            {"w": found.reader.workspace_id, "p": found.project_id},
        )
    ).one()
    return SiteOut(
        address=found.address,
        project=row.project,
        repository=row.repository,
        commit_sha=row.commit_sha,
        visibility=found.visibility,
        generated=row.estimate_id is not None,
        published_at=row.updated_at,
        license=SiteLicense(spdx_id=row.license_spdx, path=row.license_path, file_id=row.license_file)
        if row.license_spdx
        else None,
    )


@router.get("/published/{owner}/{project}/tree", response_model=TreeOut)
async def tree(
    owner: OwnerPath,
    project: ProjectPath,
    db: Db,
    found: SiteDep,
    build: SiteBuild,
    path: Annotated[str, Query(max_length=1024)] = "",
) -> TreeOut:
    return TreeOut(items=[TreeEntry(**e) for e in await service.tree(db, found.reader, build, path)])


@router.get("/published/{owner}/{project}/variants", response_model=SiteVariantsOut)
async def variants(owner: OwnerPath, project: ProjectPath, db: Db, found: SiteDep) -> SiteVariantsOut:
    return SiteVariantsOut(items=[SiteVariant(**v) for v in await site_variants(db, found)])


@router.get("/published/{owner}/{project}/files/{file_id}", response_model=FileOut)
async def file_view(
    owner: OwnerPath,
    project: ProjectPath,
    file_id: UUID,
    request: Request,
    db: Db,
    found: SiteDep,
    build: SiteBuild,
    start_line: Annotated[int, Query(ge=1)] = 1,
    end_line: Annotated[int | None, Query(ge=1)] = None,
) -> FileOut:
    return FileOut(
        **await service.file_view(db, _objects(request), found.reader, build, file_id, start_line, end_line)
    )


@router.get("/published/{owner}/{project}/symbols/{symbol_id}", response_model=SymbolOut)
async def symbol_view(
    owner: OwnerPath, project: ProjectPath, symbol_id: UUID, db: Db, found: SiteDep, build: SiteBuild
) -> SymbolOut:
    return SymbolOut(**await service.symbol_view(db, found.reader, build, symbol_id))


@router.get("/published/{owner}/{project}/guide", response_model=GuideIndexOut)
async def guide_index(
    owner: OwnerPath, project: ProjectPath, request: Request, db: Db, found: SiteDep, build: SiteBuild
) -> GuideIndexOut:
    return GuideIndexOut(**await service.guide_index(db, _objects(request), found.reader, build))


@router.get("/published/{owner}/{project}/guide/{page}", response_model=GuidePageOut)
async def guide_page(
    owner: OwnerPath,
    project: ProjectPath,
    request: Request,
    db: Db,
    found: SiteDep,
    build: SiteBuild,
    page: Annotated[str, Path(pattern=r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")],
) -> GuidePageOut:
    return GuidePageOut(**await service.guide_page(db, _objects(request), found.reader, build, page))


@router.get("/published/{owner}/{project}/search", response_model=SearchOut)
async def search(
    owner: OwnerPath,
    project: ProjectPath,
    db: Db,
    found: SiteDep,
    build: SiteBuild,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    kind: Annotated[str | None, Query(pattern=r"^(symbol|file|guide)$")] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> SearchOut:
    hits = await service.search_build(db, found.reader, build, q, kind, limit)
    return SearchOut(items=[SearchHit(**h) for h in hits])


class AliasOut(BaseModel):
    owner: str
    project: str


async def alias_target(db: Any, alias: str, user_id: UUID | None) -> Any:
    """The serving site a pre-0039 alias leads to, or None. A private site counts only for a signed-in
    person who may see its project, so an old members-only slug reveals nothing to anyone else."""
    found = (
        await db.execute(
            text(
                "SELECT owner, project, workspace_id, project_id, visibility FROM app_private.site_alias(:a)"
            ),
            {"a": alias},
        )
    ).one_or_none()
    if found is None or found.visibility == "public":
        return found
    if user_id is None:
        return None
    role = (
        await db.execute(
            text("SELECT app_private.project_role(:w, :p, :u)"),
            {"w": found.workspace_id, "p": found.project_id, "u": user_id},
        )
    ).scalar_one_or_none()
    return found if role is not None else None


@router.get("/site-aliases/{alias}", response_model=AliasOut)
async def site_alias(alias: AliasPath, request: Request, db: Db, principal: OptionalPrincipal) -> AliasOut:
    """Where a pre-0039 address (/sites/<slug>) leads now, for a year after the change: a public site for
    anyone, a private one for its members; 404 otherwise, alike for unknown, private and withdrawn."""
    settings = request.app.state.settings
    await ratelimit.hit(
        request.app.state.rate_sessions, settings, "public", ratelimit.client_address(request, settings)
    )
    found = await alias_target(db, alias, principal.user_id if principal else None)
    await db.commit()
    if found is None:
        raise ApiError(404, "NOT_FOUND", "Site not found.")
    return AliasOut(owner=found.owner, project=found.project)
