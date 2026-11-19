"""Build progress as server-sent events (06 "Server-sent events", 10, A17).

The stream replays `build_events` after the client's `Last-Event-ID` (the event sequence), then follows
new events until the build is terminal. Every frame comes from SQL, read in a short transaction with
the tenant context set; nothing is held open between polls. The caller's session and membership are
re-checked at least every 30 seconds and before the first frame: a revoked session or a removed member
ends the stream. A heartbeat comment is sent every 15 seconds. A cursor older than the retained history
gets a `reset` event pointing at the status snapshot. Streams are limited per session and per workspace
in this process; the gateway carries the cross-replica limit (22).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import metrics
from app.core.errors import ApiError
from app.core.security import digest
from app.core.tenancy import set_context

TERMINAL = ("ready", "failed", "cancelled", "budget_exhausted")
PER_SESSION, PER_WORKSPACE = 3, 20


@dataclass
class Timing:
    poll_seconds: float = 1.0
    heartbeat_seconds: float = 15.0
    recheck_seconds: float = 30.0


_open: Counter[str] = Counter()


class StreamSlot:
    """In-process concurrency limit for streams (06: 3 per session, 20 per workspace)."""

    def __init__(self, session_id: UUID, workspace_id: UUID) -> None:
        self.keys = (f"s:{session_id}", f"w:{workspace_id}")
        self.held = False

    def __enter__(self) -> StreamSlot:
        session_key, workspace_key = self.keys
        if _open[session_key] >= PER_SESSION or _open[workspace_key] >= PER_WORKSPACE:
            metrics.SSE_REJECTED.inc()
            raise ApiError(429, "STREAM_LIMIT", "Too many open event streams.", retryable=True)
        for key in self.keys:
            _open[key] += 1
        self.held = True
        metrics.SSE_STREAMS.inc()
        return self

    def __exit__(self, *exc: object) -> None:
        """Idempotent: the response and the generator may both release."""
        if not self.held:
            return
        self.held = False
        metrics.SSE_STREAMS.dec()
        for key in self.keys:
            _open[key] -= 1
            if _open[key] <= 0:
                del _open[key]


class SlotResponse(StreamingResponse):
    """Releases its stream slot when the response ends however it ends, including a client that
    disconnects before the body is ever iterated (the generator's `finally` would not run then)."""

    def __init__(self, content: AsyncIterator[str], slot: StreamSlot, **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self.slot = slot

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.slot.__exit__()


def frame(sequence: int, event_type: str, payload: dict[str, Any]) -> str:
    data = json.dumps(payload, separators=(",", ":"), default=str)
    return f"id: {sequence}\nevent: {event_type}\ndata: {data}\n\n"


async def _still_allowed(
    db: AsyncSession, token: str, user_id: UUID, workspace_id: UUID, build_id: UUID
) -> bool:
    session = (
        await db.execute(text("SELECT user_id FROM app_private.session_by_token(:t)"), {"t": digest(token)})
    ).one_or_none()
    if session is None or session.user_id != user_id:
        return False
    await set_context(db, user_id=user_id)
    member = (
        await db.execute(
            text(
                "SELECT 1 FROM memberships m JOIN workspaces w ON w.id = m.workspace_id"
                " WHERE m.workspace_id = :w AND m.user_id = :u AND w.status = 'active'"
            ),
            {"w": workspace_id, "u": user_id},
        )
    ).one_or_none()
    if member is None:
        return False
    # Per-project access (0027): losing a restricted project's grant ends the stream like losing membership.
    await set_context(db, workspace_id=workspace_id, user_id=user_id)
    allowed = (
        await db.execute(
            text(
                "SELECT app_private.project_role(b.workspace_id, b.project_id, :u) IS NOT NULL"
                " FROM builds b WHERE b.workspace_id = :w AND b.id = :b"
            ),
            {"w": workspace_id, "b": build_id, "u": user_id},
        )
    ).scalar_one_or_none()
    return bool(allowed)


async def _poll(
    db: AsyncSession, workspace_id: UUID, build_id: UUID, after: int
) -> tuple[list[Any], str, int]:
    await set_context(db, workspace_id=workspace_id)
    rows = list(
        await db.execute(
            text(
                "SELECT sequence, event_type, payload, created_at FROM build_events"
                " WHERE workspace_id = :w AND build_id = :b AND sequence > :after ORDER BY sequence LIMIT 200"
            ),
            {"w": workspace_id, "b": build_id, "after": after},
        )
    )
    status = (
        await db.execute(
            text("SELECT status FROM builds WHERE workspace_id = :w AND id = :b"),
            {"w": workspace_id, "b": build_id},
        )
    ).scalar_one()
    oldest = (
        await db.execute(
            text(
                "SELECT COALESCE(min(sequence), 0) FROM build_events WHERE workspace_id = :w AND build_id = :b"
            ),
            {"w": workspace_id, "b": build_id},
        )
    ).scalar_one()
    await db.commit()
    return rows, status, int(oldest)


async def stream(
    sessions: async_sessionmaker[AsyncSession],
    *,
    token: str,
    user_id: UUID,
    workspace_id: UUID,
    build_id: UUID,
    after: int,
    timing: Timing,
    disconnected: Any,
) -> AsyncIterator[str]:
    last = after
    yield "retry: 5000\n\n"
    next_check = 0.0
    next_beat = time.monotonic() + timing.heartbeat_seconds
    while True:
        now = time.monotonic()
        async with sessions() as db:
            if now >= next_check:
                if not await _still_allowed(db, token, user_id, workspace_id, build_id):
                    yield frame(last, "access.revoked", {"build_id": str(build_id)})
                    return
                await db.commit()
                next_check = now + timing.recheck_seconds
            rows, status, oldest = await _poll(db, workspace_id, build_id, last)
        if last and oldest > last + 1:  # the cursor is older than the retained history (30 days)
            yield frame(last, "reset", {"build_id": str(build_id), "snapshot": f"/api/v1/builds/{build_id}"})
            return
        for row in rows:
            last = row.sequence
            yield frame(
                row.sequence, row.event_type, {**row.payload, "build_id": str(build_id), "at": row.created_at}
            )
        if status in TERMINAL and not rows:
            return
        if now >= next_beat:
            yield ": heartbeat\n\n"
            next_beat = now + timing.heartbeat_seconds
        if await disconnected():
            return
        await asyncio.sleep(timing.poll_seconds)
