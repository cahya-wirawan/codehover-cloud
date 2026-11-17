"""Connecting a GitHub App installation to a workspace, and reading repositories through it.

Connect flow (09): admin starts -> GitHub installation -> setup URL (installation_id, untrusted) ->
GitHub App user authorization (PKCE) -> callback: the App's user token must belong to the GitHub
identity linked to this account, and the installation must be in that user's `/user/installations`.
Each phase's state is single-use, browser-bound and sealed with its phase, so states cannot be replayed
across phases. No database transaction is held open across a GitHub call (04).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.github.app import GitHubApp, GitHubError
from app.core.errors import ApiError
from app.core.security import Sealer, digest, new_token, pkce_pair
from app.core.settings import Settings
from app.core.tenancy import set_context
from app.modules.identity.sessions import Principal
from app.modules.workspaces.access import Member
from app.modules.workspaces.service import audit

PROVIDER = "github_app"
STATE_TTL_SECONDS = 600


class ConnectError(Exception):
    """The connect flow stopped. `code` goes to the connections page; `workspace_id` if known."""

    def __init__(self, code: str, workspace_id: UUID | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.workspace_id = workspace_id


def callback_uri(settings: Settings) -> str:
    return f"{settings.public_origin}/api/v1/integrations/github/callback"


async def _create_state(
    db: AsyncSession, sealer: Sealer, *, session_id: UUID, workspace_id: UUID, payload: dict[str, Any]
) -> tuple[str, str]:
    state, binding = new_token(), new_token()
    state_hash = digest(state)
    await db.execute(
        text(
            "SELECT app_private.oauth_transaction_create(:state, :binding, :provider, 'install', NULL,"
            " :sealed, '/', :session, :workspace, make_interval(secs => :ttl))"
        ),
        {
            "state": state_hash,
            "binding": digest(binding),
            "provider": PROVIDER,
            "sealed": sealer.seal(json.dumps(payload).encode(), state_hash),
            "session": session_id,
            "workspace": workspace_id,
            "ttl": STATE_TTL_SECONDS,
        },
    )
    await db.commit()
    return state, binding


async def _consume_state(
    db: AsyncSession,
    sealer: Sealer,
    principal: Principal | None,
    *,
    state: str,
    binding: str | None,
    phase: str,
) -> tuple[UUID, dict[str, Any]]:
    if not binding:
        raise ConnectError("expired")
    state_hash = digest(state)
    row = (
        await db.execute(
            text(
                "SELECT purpose, encrypted_pkce_verifier, initiating_session_id, workspace_id"
                " FROM app_private.oauth_transaction_consume(:state, :binding, :provider)"
            ),
            {"state": state_hash, "binding": digest(binding), "provider": PROVIDER},
        )
    ).one_or_none()
    await db.commit()
    if row is None or row.purpose != "install" or row.workspace_id is None:
        raise ConnectError("expired")
    payload: dict[str, Any] = json.loads(sealer.open(row.encrypted_pkce_verifier, state_hash))
    if payload.get("phase") != phase:
        raise ConnectError("expired", row.workspace_id)
    # The flow must finish in the session that started it (09).
    if principal is None or principal.session_id != row.initiating_session_id:
        raise ConnectError("expired", row.workspace_id)
    workspace_id: UUID = row.workspace_id
    return workspace_id, payload


# ---------------------------------------------------------------- connect flow
async def begin_install(db: AsyncSession, sealer: Sealer, app: GitHubApp, member: Member) -> tuple[str, str]:
    state, binding = await _create_state(
        db,
        sealer,
        session_id=member.principal.session_id,
        workspace_id=member.workspace_id,
        payload={"phase": "install"},
    )
    return app.installation_url(state), binding


async def continue_after_install(
    db: AsyncSession,
    settings: Settings,
    sealer: Sealer,
    app: GitHubApp,
    principal: Principal | None,
    *,
    state: str,
    binding: str | None,
    installation_id: int,
) -> tuple[str, str, UUID]:
    """Setup URL: remember the (untrusted) installation ID and send the user to App authorization."""
    workspace_id, _ = await _consume_state(
        db, sealer, principal, state=state, binding=binding, phase="install"
    )
    assert principal is not None
    verifier, challenge = pkce_pair()
    next_state, next_binding = await _create_state(
        db,
        sealer,
        session_id=principal.session_id,
        workspace_id=workspace_id,
        payload={"phase": "authorize", "verifier": verifier, "installation_id": installation_id},
    )
    url = app.user_authorization_url(
        state=next_state, code_challenge=challenge, redirect_uri=callback_uri(settings)
    )
    return url, next_binding, workspace_id


async def finish_connection(
    db: AsyncSession,
    settings: Settings,
    sealer: Sealer,
    app: GitHubApp,
    principal: Principal | None,
    *,
    state: str,
    binding: str | None,
    code: str,
) -> UUID:
    workspace_id, payload = await _consume_state(
        db, sealer, principal, state=state, binding=binding, phase="authorize"
    )
    assert principal is not None
    installation_id = int(payload["installation_id"])

    await set_context(db, user_id=principal.user_id)
    linked = {
        r.subject
        for r in await db.execute(
            text("SELECT subject FROM oauth_identities WHERE user_id = :u AND provider = 'github'"),
            {"u": principal.user_id},
        )
    }
    await db.commit()
    if not linked:
        raise ConnectError("github_identity_required", workspace_id)

    try:  # external calls: no transaction open
        user_token = await app.exchange_user_code(
            code=code, code_verifier=payload["verifier"], redirect_uri=callback_uri(settings)
        )
        github_user = await app.user_id(user_token)
        installations = await app.user_installations(user_token)
    except GitHubError as error:
        raise ConnectError("provider", workspace_id) from error
    if str(github_user) not in linked:
        raise ConnectError("identity_mismatch", workspace_id)
    installation = next((i for i in installations if i.id == installation_id), None)
    if installation is None:  # spoofed ID, or someone else's installation
        raise ConnectError("installation_not_accessible", workspace_id)

    # Re-check authority now: the user must still be an admin of the workspace.
    await set_context(db, user_id=principal.user_id)  # context is transaction-local; the commit cleared it
    role = (
        await db.execute(
            text("SELECT role FROM memberships WHERE workspace_id = :w AND user_id = :u"),
            {"w": workspace_id, "u": principal.user_id},
        )
    ).scalar_one_or_none()
    if role not in ("admin", "owner"):
        await db.rollback()
        raise ConnectError("forbidden", workspace_id)
    await set_context(db, workspace_id=workspace_id, user_id=principal.user_id)
    connection_id = (
        await db.execute(
            text(
                "INSERT INTO repository_connections(workspace_id, installation_id, github_account_id,"
                " github_account_login, connected_by) VALUES (:w, :i, :aid, :login, :u)"
                " ON CONFLICT (workspace_id, installation_id) DO UPDATE SET status = 'active',"
                " github_account_id = EXCLUDED.github_account_id,"
                " github_account_login = EXCLUDED.github_account_login,"
                " connected_by = EXCLUDED.connected_by, last_verified_at = now(), updated_at = now()"
                " RETURNING id"
            ),
            {
                "w": workspace_id,
                "i": installation.id,
                "aid": installation.account_id,
                "login": installation.account_login,
                "u": principal.user_id,
            },
        )
    ).scalar_one()
    member = Member(principal, workspace_id, role)
    await audit(
        db,
        member,
        "connection.created",
        "repository_connection",
        connection_id,
        {"installation_id": installation.id, "account": installation.account_login},
    )
    await db.commit()
    return workspace_id


# ---------------------------------------------------------------- connections
async def list_connections(db: AsyncSession, member: Member) -> list[dict[str, Any]]:
    rows = await db.execute(
        text(
            "SELECT id, installation_id, github_account_login AS account, status, last_verified_at,"
            " created_at"
            " FROM repository_connections WHERE workspace_id = :w ORDER BY created_at, id"
        ),
        {"w": member.workspace_id},
    )
    return [dict(r._mapping) for r in rows]


async def revoke_connection(db: AsyncSession, member: Member, connection_id: UUID) -> None:
    row = (
        await db.execute(
            text(
                "UPDATE repository_connections SET status = 'revoked', updated_at = now()"
                " WHERE workspace_id = :w AND id = :c AND status <> 'revoked' RETURNING installation_id"
            ),
            {"w": member.workspace_id, "c": connection_id},
        )
    ).one_or_none()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Connection not found.")
    await audit(db, member, "connection.revoked", "repository_connection", connection_id)
    await db.commit()


@dataclass(frozen=True)
class _Connection:
    id: UUID
    installation_id: int


async def _active_connection(db: AsyncSession, member: Member, connection_id: UUID) -> _Connection:
    row = (
        await db.execute(
            text(
                "SELECT id, installation_id, status FROM repository_connections"
                " WHERE workspace_id = :w AND id = :c"
            ),
            {"w": member.workspace_id, "c": connection_id},
        )
    ).one_or_none()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Connection not found.")
    if row.status != "active":
        raise ApiError(409, "CONNECTION_INACTIVE", "Reconnect GitHub to access these repositories.")
    return _Connection(row.id, row.installation_id)


def _github_failure(error: GitHubError) -> ApiError:
    if error.status in (403, 404):
        return ApiError(404, "NOT_FOUND", "Repository not found.")
    if error.reason == "installation_token_failed":
        return ApiError(409, "CONNECTION_INACTIVE", "Reconnect GitHub to access these repositories.")
    return ApiError(503, "UNAVAILABLE", "GitHub is not reachable right now.", retryable=True)


# ---------------------------------------------------------------- repositories and refs
async def list_repositories(
    db: AsyncSession, app: GitHubApp, member: Member, connection_id: UUID
) -> list[dict[str, Any]]:
    connection = await _active_connection(db, member, connection_id)
    await db.commit()
    try:
        repos = await app.installation_repositories(connection.installation_id)
    except GitHubError as error:
        raise _github_failure(error) from error
    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    results = []
    for repo in repos:
        row = (
            await db.execute(
                text(
                    "INSERT INTO repositories(workspace_id, connection_id, provider_repo_id, owner, name,"
                    " visibility, default_branch) VALUES (:w, :c, :rid, :o, :n, :v, :b)"
                    " ON CONFLICT (workspace_id, provider_repo_id) DO UPDATE"
                    " SET connection_id = EXCLUDED.connection_id,"
                    " owner = EXCLUDED.owner, name = EXCLUDED.name, visibility = EXCLUDED.visibility,"
                    " default_branch = EXCLUDED.default_branch, updated_at = now()"
                    " RETURNING id"
                ),
                {
                    "w": member.workspace_id,
                    "c": connection.id,
                    "rid": repo.id,
                    "o": repo.owner,
                    "n": repo.name,
                    "v": repo.visibility,
                    "b": repo.default_branch,
                },
            )
        ).scalar_one()
        results.append(
            {
                "id": row,
                "provider_repo_id": repo.id,
                "owner": repo.owner,
                "name": repo.name,
                "visibility": repo.visibility,
                "default_branch": repo.default_branch,
            }
        )
    await db.commit()
    return results


async def list_refs(
    db: AsyncSession, app: GitHubApp, member: Member, repository_id: UUID
) -> list[dict[str, Any]]:
    row = (
        await db.execute(
            text(
                "SELECT connection_id, provider_repo_id FROM repositories WHERE workspace_id = :w AND id = :r"
            ),
            {"w": member.workspace_id, "r": repository_id},
        )
    ).one_or_none()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Repository not found.")
    connection = await _active_connection(db, member, row.connection_id)
    await db.commit()
    try:
        refs = await app.refs(connection.installation_id, row.provider_repo_id)
    except GitHubError as error:
        raise _github_failure(error) from error
    return [{"name": r.name, "kind": r.kind, "sha": r.sha} for r in refs]
