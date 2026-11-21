"""Send due owner notices (run by the worker's maintenance loop).

Per notice, inside its workspace's context:
1. claim: lock the row (SKIP LOCKED), count the attempt and push `next_attempt_at` out as a lease, read
   the recipients (active owners and admins with an email) and write the messages; commit;
2. send with no transaction open;
3. record the outcome only if the claim still holds (state `sending`, same attempt).

Delivery is at least once: a crash between sending and recording sends the notice again on the next
claim, with the same Message-IDs. Notices of a workspace being deleted, notices older than
`MAX_AGE_HOURS` (an old suspension is no news) and notices without SMTP configured are not sent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.email import Mailer, MailError, OutgoingEmail
from app.core import metrics
from app.core.settings import Settings
from app.core.tenancy import set_context
from app.modules.notices import messages

logger = logging.getLogger("app.notices")

MAX_ATTEMPTS = 8
MAX_AGE_HOURS = 72
BATCH = 50


def backoff(attempt: int) -> timedelta:
    """Lease and retry delay after attempt n (1-based): 1, 2, 4 ... minutes, at most 2 hours."""
    return timedelta(minutes=min(2 ** (attempt - 1), 120))


@dataclass(frozen=True)
class _Claim:
    attempt: int
    emails: list[OutgoingEmail]


def _domain(settings: Settings) -> str:
    sender = settings.smtp_from or ""
    if "@" in sender:
        return sender.rsplit("@", 1)[1].strip(" >") or "localhost"
    return urlsplit(settings.public_origin).hostname or "localhost"


async def _finish(
    db: AsyncSession, notice_id: UUID, state: str, *, error: str | None = None, recipients: int | None = None
) -> None:
    await db.execute(
        text(
            "UPDATE owner_notices SET state = :s, last_error = :e, recipients = COALESCE(:r, recipients),"
            " sent_at = CASE WHEN :s = 'sent' THEN now() ELSE sent_at END WHERE id = :id"
        ),
        {"s": state, "e": error, "r": recipients, "id": notice_id},
    )
    metrics.NOTICES.labels(outcome=metrics.label(state if error is None else error)).inc()


async def _claim(
    db: AsyncSession, settings: Settings, workspace_id: UUID, notice_id: UUID, *, mailer: bool
) -> _Claim | None:
    row = (
        await db.execute(
            text(
                "SELECT n.id, n.kind, n.details, n.attempts, n.created_at, w.name, w.status"
                " FROM owner_notices n JOIN workspaces w ON w.id = n.workspace_id"
                " WHERE n.workspace_id = :w AND n.id = :id AND n.state IN ('pending', 'sending')"
                " AND n.next_attempt_at <= now() FOR UPDATE OF n SKIP LOCKED"
            ),
            {"w": workspace_id, "id": notice_id},
        )
    ).one_or_none()
    if row is None:
        return None  # sent, or claimed by another worker
    if row.status == "deleting":
        await _finish(db, notice_id, "skipped", error="workspace_deleting")
        return None
    if not mailer:
        await _finish(db, notice_id, "skipped", error="email_not_configured")
        return None
    if row.created_at < datetime.now(UTC) - timedelta(hours=MAX_AGE_HOURS):
        await _finish(db, notice_id, "failed", error="expired")
        return None
    if row.attempts >= MAX_ATTEMPTS:
        await _finish(db, notice_id, "failed", error="attempts_exhausted")
        return None
    recipients = [
        r.primary_email
        for r in await db.execute(
            text(
                "SELECT DISTINCT u.primary_email FROM memberships m JOIN users u ON u.id = m.user_id"
                " WHERE m.workspace_id = :w AND m.role IN ('owner', 'admin') AND u.status = 'active'"
                " AND u.deleted_at IS NULL AND u.primary_email IS NOT NULL ORDER BY u.primary_email"
            ),
            {"w": workspace_id},
        )
        if "@" in r.primary_email and "\n" not in r.primary_email and "\r" not in r.primary_email
    ]
    if not recipients:
        await _finish(db, notice_id, "skipped", error="no_recipients", recipients=0)
        return None
    details: dict[str, Any] = row.details or {}
    projects: list[tuple[str, str]] = []
    if row.kind == "site_suspended":
        ids = [str(p) for p in details.get("project_ids") or []]
        projects = [
            (str(p.id), p.name)
            for p in await db.execute(
                text(
                    "SELECT id, name FROM projects WHERE workspace_id = :w AND id = ANY(CAST(:ids AS uuid[]))"
                    " AND deleted_at IS NULL ORDER BY name"
                ),
                {"w": workspace_id, "ids": ids},
            )
        ]
        if not projects:
            await _finish(db, notice_id, "skipped", error="projects_gone")
            return None
    attempt = row.attempts + 1
    await db.execute(
        text(
            "UPDATE owner_notices SET state = 'sending', attempts = :a, next_attempt_at = :next WHERE id = :id"
        ),
        {"a": attempt, "next": datetime.now(UTC) + backoff(attempt), "id": notice_id},
    )
    context = messages.Context(
        origin=settings.public_origin,
        workspace_id=str(workspace_id),
        workspace_name=row.name,
        projects=projects,
        support=settings.support_email,
    )
    subject = messages.subject(row.kind, settings.operator_name)
    body = messages.body(row.kind, details, context)
    domain = _domain(settings)
    return _Claim(
        attempt,
        [
            OutgoingEmail(to, subject, body, f"notice-{notice_id}-{n}@{domain}")
            for n, to in enumerate(recipients)
        ],
    )


async def deliver_one(
    sessions: async_sessionmaker[AsyncSession],
    mailer: Mailer | None,
    settings: Settings,
    workspace_id: UUID,
    notice_id: UUID,
) -> str | None:
    async with sessions() as db:
        await set_context(db, workspace_id=workspace_id)
        claim = await _claim(db, settings, workspace_id, notice_id, mailer=mailer is not None)
        await db.commit()
    if claim is None or mailer is None:
        return None
    outcome: tuple[str, str | None, int | None]
    try:
        result = await mailer.send(claim.emails)  # no transaction open (04)
        if result.delivered:
            outcome = ("sent", None, result.delivered)
        else:
            outcome = ("failed", "recipients_refused", 0)
    except MailError as error:
        logger.warning("notice not sent", extra={"notice_id": str(notice_id), "code": error.code})
        if error.transient and claim.attempt < MAX_ATTEMPTS:
            outcome = ("pending", error.code, None)
        else:
            outcome = ("failed", error.code, None)
    state, code, count = outcome
    async with sessions() as db:
        await set_context(db, workspace_id=workspace_id)
        held = (
            await db.execute(
                text(
                    "SELECT id FROM owner_notices WHERE workspace_id = :w AND id = :id"
                    " AND state = 'sending' AND attempts = :a FOR UPDATE"
                ),
                {"w": workspace_id, "id": notice_id, "a": claim.attempt},
            )
        ).scalar_one_or_none()
        if held is not None:
            await _finish(db, notice_id, state, error=code, recipients=count)
        await db.commit()
    return state


async def deliver_due(
    sessions: async_sessionmaker[AsyncSession], mailer: Mailer | None, settings: Settings
) -> dict[str, int]:
    async with sessions() as db:
        due = list(await db.execute(text("SELECT * FROM app_private.due_owner_notices(:n)"), {"n": BATCH}))
        await db.commit()
    counts: dict[str, int] = {}
    for row in due:
        try:
            state = await deliver_one(sessions, mailer, settings, row.workspace_id, row.notice_id)
        except Exception:
            logger.exception("notice delivery failed", extra={"notice_id": str(row.notice_id)})
            continue
        if state is not None:
            counts[state] = counts.get(state, 0) + 1
    return counts
