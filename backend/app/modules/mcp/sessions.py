"""MCP sessions and their stream (0043; MCP Streamable HTTP, 06 "/mcp/{owner}/{project}").

`initialize` opens a session and answers with `Mcp-Session-Id` (a random token; only its SHA-256 digest
is stored). A session belongs to the site's project and to the person whose token opened it (none on a
public site), so another person's token cannot use it. Requests without the header still work
statelessly, as before. A session holds up to 50 resource subscriptions (`resources/subscribe`).

`GET` with the session opens a server-sent event stream. Every frame comes from SQL read in a short
transaction; nothing is held open between polls. When the site's publication changes (a new build, a
rollback, other approved languages, a visibility change), the stream sends
`notifications/resources/list_changed`, then `notifications/resources/updated` for each subscribed URI
whose content differs. When the site stops being served to this reader (withdrawn, suspended, deleted,
the token revoked or the person removed), it sends `list_changed` once more and ends. The reader's access
is re-checked at least every 30 seconds, a heartbeat comment goes out every 15, and a stream ends after
15 minutes (clients reconnect). Streams are limited per session and per workspace in this process, like
build streams (22).

Sessions end after a day without use and a week at most; `DELETE` ends one at once.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.storage import ObjectStore
from app.core.security import digest
from app.core.tenancy import set_context
from app.modules.mcp import resources, tokens
from app.modules.published.router import Site, find_site
from app.modules.workspaces.access import PublicReader

HEADER = "Mcp-Session-Id"
MAX_SUBSCRIPTIONS = 50  # also the table's CHECK; sessions last a day idle and a week at most (0043)


@dataclass(frozen=True)
class Session:
    id: UUID
    workspace_id: UUID
    project_id: UUID
    user_id: UUID | None
    subscriptions: tuple[str, ...]


@dataclass
class Timing:
    poll_seconds: float = 2.0
    heartbeat_seconds: float = 15.0
    recheck_seconds: float = 30.0
    max_seconds: float = 15 * 60


async def open_session(
    db: AsyncSession, workspace_id: UUID, project_id: UUID, user_id: UUID | None, protocol: str
) -> str:
    """A new session's token (the caller set the workspace context and commits)."""
    token = secrets.token_urlsafe(32)
    await db.execute(
        text(
            "INSERT INTO mcp_sessions(workspace_id, project_id, session_hash, user_id, protocol_version, expires_at)"
            " VALUES (:w, :p, :h, :u, :v, now() + interval '24 hours')"
        ),
        {"w": workspace_id, "p": project_id, "h": digest(token), "u": user_id, "v": protocol},
    )
    return token


async def find(
    db: AsyncSession, workspace_id: UUID, project_id: UUID, token: str, user_id: UUID | None
) -> Session | None:
    """The live session of this site and person named by `token`, kept alive; None when it is unknown,
    ended, of another site or of another person (all alike: MCP clients then start a new one)."""
    if not token or len(token) > 100:
        return None
    row = (
        await db.execute(
            text(
                "SELECT id, user_id, subscriptions FROM mcp_sessions WHERE workspace_id = :w AND project_id = :p"
                " AND session_hash = :h AND expires_at > now()"
            ),
            {"w": workspace_id, "p": project_id, "h": digest(token)},
        )
    ).one_or_none()
    if row is None or row.user_id != user_id:
        return None
    await db.execute(
        text(
            "UPDATE mcp_sessions SET last_seen_at = now(),"
            " expires_at = LEAST(created_at + interval '7 days', now() + interval '24 hours')"
            " WHERE workspace_id = :w AND id = :id AND last_seen_at < now() - interval '1 minute'"
        ),
        {"w": workspace_id, "id": row.id},
    )
    return Session(row.id, workspace_id, project_id, row.user_id, tuple(row.subscriptions))


async def subscribe(db: AsyncSession, session: Session, uri: str) -> bool:
    """Add a subscription; False when the session already has the most it may hold."""
    if uri in session.subscriptions:
        return True
    if len(session.subscriptions) >= MAX_SUBSCRIPTIONS:
        return False
    await db.execute(
        text(
            "UPDATE mcp_sessions SET subscriptions = array_append(subscriptions, :u)"
            " WHERE workspace_id = :w AND id = :id AND NOT (:u = ANY(subscriptions))"
            " AND cardinality(subscriptions) < :n"
        ),
        {"u": uri, "w": session.workspace_id, "id": session.id, "n": MAX_SUBSCRIPTIONS},
    )
    return True


async def unsubscribe(db: AsyncSession, session: Session, uri: str) -> None:
    await db.execute(
        text(
            "UPDATE mcp_sessions SET subscriptions = array_remove(subscriptions, :u) WHERE workspace_id = :w AND id = :id"
        ),
        {"u": uri, "w": session.workspace_id, "id": session.id},
    )


async def end(db: AsyncSession, session: Session) -> None:
    await db.execute(
        text("DELETE FROM mcp_sessions WHERE workspace_id = :w AND id = :id"),
        {"w": session.workspace_id, "id": session.id},
    )


def notification(method: str, params: dict[str, Any] | None = None) -> str:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return f"event: message\ndata: {json.dumps(message, separators=(',', ':'))}\n\n"


async def _readable(db: AsyncSession, found: Any, project_id: UUID, bearer: str | None) -> bool:
    """Whether the site is still served to this reader: public, or a member's token that may see it."""
    if found is None or found.project_id != project_id:
        return False
    if found.visibility == "public":
        return True
    principal = await tokens.resolve(db, bearer) if bearer else None
    if principal is None or principal.workspace_id != found.workspace_id:
        return False
    if principal.project_id is not None and principal.project_id != found.project_id:
        return False
    role = (
        await db.execute(
            text("SELECT app_private.project_role(:w, :p, :u)"),
            {"w": found.workspace_id, "p": found.project_id, "u": principal.user_id},
        )
    ).scalar_one_or_none()
    return role is not None


async def _state(db: AsyncSession, workspace_id: UUID, project_id: UUID) -> tuple[Any, ...] | None:
    """What a change of the site is measured by: its publication's version and builds."""
    row = (
        await db.execute(
            text(
                "SELECT version, build_id, variant_build_ids, visibility FROM publications"
                " WHERE workspace_id = :w AND project_id = :p"
            ),
            {"w": workspace_id, "p": project_id},
        )
    ).one_or_none()
    return (
        None
        if row is None
        else (row.version, row.build_id, tuple(row.variant_build_ids or ()), row.visibility)
    )


async def _content(db: AsyncSession, objects: ObjectStore, site: Site, uri: str) -> str | None:
    try:
        contents = await resources.read(db, objects, site, site.address, uri)
    except resources.NotFound:
        return None
    return hashlib.sha256(json.dumps(contents, sort_keys=True).encode()).hexdigest()


async def follow(
    sessions: async_sessionmaker[AsyncSession],
    objects: ObjectStore,
    address: str,
    session: Session,
    bearer: str | None,
    timing: Timing,
) -> AsyncIterator[str]:
    """The stream of one session (see the module docstring)."""
    owner, _, project = address.partition("/")
    started = last_beat = last_check = time.monotonic()
    seen: tuple[Any, ...] | None = None
    hashes: dict[str, str | None] = {}
    yield ": stream open\n\n"
    while time.monotonic() - started < timing.max_seconds:
        now = time.monotonic()
        recheck = now - last_check >= timing.recheck_seconds or seen is None
        async with sessions() as db:
            found = await find_site(db, owner, project)
            if recheck:
                readable = await _readable(db, found, session.project_id, bearer)
                last_check = now
            else:
                readable = found is not None and found.project_id == session.project_id
            if not readable:
                await db.commit()
                yield notification("notifications/resources/list_changed")
                return
            await set_context(db, workspace_id=session.workspace_id)
            row = (
                await db.execute(
                    text(
                        "SELECT subscriptions FROM mcp_sessions WHERE workspace_id = :w AND id = :id AND expires_at > now()"
                    ),
                    {"w": session.workspace_id, "id": session.id},
                )
            ).one_or_none()
            if row is None:  # the session ended (DELETE, expiry)
                await db.commit()
                return
            state = await _state(db, session.workspace_id, session.project_id)
            site = Site(
                PublicReader(found.workspace_id),
                found.project_id,
                found.build_id,
                found.visibility,
                f"{found.owner}/{found.project}",
            )
            subscribed = list(row.subscriptions)
            changed = seen is not None and state != seen
            updated = []
            for uri in subscribed:
                if uri not in hashes or changed:
                    value = await _content(db, objects, site, uri)
                    if changed and uri in hashes and value != hashes[uri]:
                        updated.append(uri)
                    hashes[uri] = value
            for uri in [u for u in hashes if u not in subscribed]:
                del hashes[uri]
            await db.commit()
        if changed:
            yield notification("notifications/resources/list_changed")
            for uri in updated:
                yield notification("notifications/resources/updated", {"uri": uri})
        seen = state
        if time.monotonic() - last_beat >= timing.heartbeat_seconds:
            last_beat = time.monotonic()
            yield ": ping\n\n"
        await asyncio.sleep(timing.poll_seconds)
