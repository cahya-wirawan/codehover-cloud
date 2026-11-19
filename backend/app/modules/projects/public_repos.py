"""Projects from public GitHub repositories, without the GitHub App (01 "Public repository source", 08).

Any editor may add a repository that GitHub reports as public. It is held under the workspace's one
`public` connection (0028) and addressed by GitHub's numeric ID. If the App already grants that repository
to the workspace, the App's repository row is reused, so a repository is never read two ways at once; a
later App grant moves a public row to the App connection (`github.service._upsert_repository`).
Visibility is checked again whenever a ref is resolved or source is downloaded
(`adapters.github.public`), so a repository that turns private stops building. There are no webhooks
without the App: public projects refresh by starting a build, never by push.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.github.app import GitHubError, RepositoryInfo
from app.adapters.github.public import PublicGitHub, parse_repository
from app.core.errors import ApiError
from app.core.tenancy import set_context
from app.modules.projects import service
from app.modules.projects.estimates import public_unavailable
from app.modules.workspaces.access import Member


async def _public_connection(db: AsyncSession, member: Member) -> UUID:
    """The workspace's single public connection, created on first use."""
    await db.execute(
        text(
            "INSERT INTO repository_connections(workspace_id, provider, connected_by)"
            " VALUES (:w, 'public', :u) ON CONFLICT (workspace_id) WHERE provider = 'public' DO NOTHING"
        ),
        {"w": member.workspace_id, "u": member.user_id},
    )
    connection: UUID = (
        await db.execute(
            text("SELECT id FROM repository_connections WHERE workspace_id = :w AND provider = 'public'"),
            {"w": member.workspace_id},
        )
    ).scalar_one()
    return connection


async def _repository_row(db: AsyncSession, member: Member, repo: RepositoryInfo) -> tuple[UUID, str]:
    """(repository ID, provider to build through): the App's row when it grants the repository, else a
    row under the public connection."""
    existing = (
        await db.execute(
            text(
                "SELECT r.id, c.provider, c.status, r.access_revoked_at FROM repositories r"
                " JOIN repository_connections c ON c.workspace_id = r.workspace_id AND c.id = r.connection_id"
                " WHERE r.workspace_id = :w AND r.provider_repo_id = :rid FOR UPDATE OF r"
            ),
            {"w": member.workspace_id, "rid": repo.id},
        )
    ).one_or_none()
    if (
        existing is not None
        and existing.provider == "github"
        and existing.status == "active"
        and existing.access_revoked_at is None
    ):
        return existing.id, "github"  # the App already grants it: one way to read a repository
    connection = await _public_connection(db, member)
    params = {
        "w": member.workspace_id,
        "c": connection,
        "rid": repo.id,
        "o": repo.owner,
        "n": repo.name,
        "b": repo.default_branch,
    }
    if existing is not None:  # a public row, or an App row whose access ended: read it as public now
        await db.execute(
            text(
                "UPDATE repositories SET connection_id = :c, owner = :o, name = :n, visibility = 'public',"
                " default_branch = :b, access_revoked_at = NULL, updated_at = now()"
                " WHERE workspace_id = :w AND id = :id"
            ),
            {**params, "id": existing.id},
        )
        return existing.id, "public"
    row: UUID = (
        await db.execute(
            text(
                "INSERT INTO repositories(workspace_id, connection_id, provider_repo_id, owner, name, visibility,"
                " default_branch) VALUES (:w, :c, :rid, :o, :n, 'public', :b) RETURNING id"
            ),
            params,
        )
    ).scalar_one()
    return row, "public"


async def create_public_project(
    db: AsyncSession,
    client: PublicGitHub,
    member: Member,
    repository: str,
    name: str,
    slug: str,
    tracked_ref: str,
) -> dict[str, Any]:
    try:
        owner, repo_name = parse_repository(repository)
    except ValueError as error:
        raise ApiError(422, "REPOSITORY_INVALID", "Enter a GitHub repository as owner/name.") from error
    await db.commit()  # no transaction is open during the GitHub call
    try:
        info = await client.lookup(owner, repo_name)
    except GitHubError as error:
        raise public_unavailable(error) from error
    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    repository_id, provider = await _repository_row(db, member, info)
    return await service.create_project(db, member, repository_id, name, slug, tracked_ref, provider=provider)
