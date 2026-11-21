"""Owner notices (11 "Owner notice"): recorded once per state change, emailed to owners and admins.

Billing is replayed by `FakeStripe` (test_billing); email by `RecordingMailer`. Tests run the worker's
delivery step (`delivery.deliver_due`) directly, as the maintenance loop would.
"""

from __future__ import annotations

import secrets
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from app.adapters.email import MailError, RecordingMailer
from app.modules.billing import sync
from app.modules.notices import delivery
from tests.integration.auth_support import FakeIdP, ddl
from tests.integration.conftest import MIGRATION_URL, engine, needs_db
from tests.integration.test_billing import Billing, billing, deliver, event  # noqa: F401
from tests.integration.test_github import FakeGitHub, github, workspace  # noqa: F401
from tests.integration.test_publication import publish, ready_build
from tests.integration.test_workspaces import NewUser, join, new_user  # noqa: F401

pytestmark = [pytest.mark.integration, needs_db]


@pytest.fixture(autouse=True)
def _quiet() -> None:
    """Notices other tests left pending (nothing delivers them there) stay out of these counts."""
    ddl("UPDATE owner_notices SET state = 'skipped' WHERE state IN ('pending', 'sending')")


def notices(ws: str) -> list[Any]:
    return ddl(
        "SELECT kind, dedupe_key, details, state, attempts, last_error, recipients FROM owner_notices"
        " WHERE workspace_id = :w ORDER BY created_at, id",
        w=ws,
    )


def owner_email(ws: str) -> str:
    """Give the workspace's owner an address (the sign-in fixture's owner has none) and return it."""
    email = f"owner-{secrets.token_hex(4)}@example.test"
    ddl(
        "UPDATE users SET primary_email = :e WHERE id IN"
        " (SELECT user_id FROM memberships WHERE workspace_id = :w AND role = 'owner')",
        e=email,
        w=ws,
    )
    return email


def settings_of(billing: Billing) -> Any:
    return billing.app.state.settings.model_copy(
        update={"smtp_host": "relay.example.test", "smtp_from": "codehover <notices@example.test>"}
    )


async def test_billing_and_suspensions_notify_owners_and_admins_once(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, billing: Billing, new_user: NewUser
) -> None:
    owner, ws, project, build, _app = await ready_build(client, idp, github)
    assert (await publish(owner, project, build_id=build)).status_code == 200
    admin, member = await new_user(), await new_user()
    await join(owner, ws, admin, "admin")
    await join(owner, ws, member, "editor")
    customer = billing.customer_for(ws)
    sub = billing.fake.subscribe(customer, status="past_due")
    for _ in range(2):  # a repeated failure is the same overdue spell: one notice
        await deliver(client, event("invoice.payment_failed", {"customer": customer}))
        await billing.process()
    await billing.reconcile()
    assert [n.kind for n in notices(ws)] == ["billing_past_due"]

    sessions = billing.app.state.sessions
    ddl(
        "UPDATE subscriptions SET past_due_since = now() - interval '8 days' WHERE provider_subscription_id = :s",
        s=sub,
    )
    assert await sync.sweep_hosting(sessions, enforced=False) >= 1
    await sync.sweep_hosting(sessions, enforced=False)  # already suspended: nothing new
    suspended = notices(ws)[1]
    assert suspended.kind == "site_suspended"
    assert suspended.details == {"reason": "billing", "project_ids": [project]}

    mailer = RecordingMailer()
    settings = settings_of(billing)
    owner_address = owner_email(ws)
    assert await delivery.deliver_due(sessions, mailer, settings) == {"sent": 2}
    assert [(n.state, n.recipients) for n in notices(ws)] == [("sent", 2), ("sent", 2)]
    recipients = {owner_address, admin.email}  # type: ignore[attr-defined]
    assert {m.to for m in mailer.sent} == recipients  # owners and admins; never editors or viewers
    assert member.email not in {m.to for m in mailer.sent}  # type: ignore[attr-defined]
    site = next(m for m in mailer.sent if "stopped serving" in m.subject)
    assert site.subject.startswith("[") and "\n" not in site.subject
    name = ddl("SELECT name FROM projects WHERE id = :p", p=project)[0][0]
    assert f"- {name}: {settings.public_origin}/p/{project}" in site.body
    assert "past due for too long" in site.body
    assert len({m.message_id for m in mailer.sent}) == 4
    assert all(m.message_id.endswith("@example.test") for m in mailer.sent)
    assert await delivery.deliver_due(sessions, mailer, settings) == {}  # sent once
    assert len(mailer.sent) == 4

    # Paid, then overdue again: a new spell is a new notice.
    billing.fake.update(sub, status="active")
    await deliver(client, event("invoice.paid", {"customer": customer}))
    await billing.process()
    billing.fake.update(sub, status="past_due")
    await deliver(client, event("invoice.payment_failed", {"customer": customer}))
    await billing.process()
    assert [n.kind for n in notices(ws)].count("billing_past_due") == 2

    # Disputes: one notice when opened, however many events repeat it, and one when lost.
    charge = "ch_" + secrets.token_hex(6)
    billing.fake.charges[charge] = customer
    dispute = f"dp_{secrets.token_hex(6)}"
    billing.fake.disputes[dispute] = {"status": "needs_response", "charge": charge}
    for kind in ("charge.dispute.created", "charge.dispute.updated"):
        await deliver(client, event(kind, {"id": dispute, "charge": charge, "status": "needs_response"}))
        await billing.process()
    billing.fake.disputes[dispute]["status"] = "lost"
    for _ in range(2):
        await deliver(
            client, event("charge.dispute.closed", {"id": dispute, "charge": charge, "status": "lost"})
        )
        await billing.process()
    kinds = [n.kind for n in notices(ws)]
    assert kinds.count("dispute_opened") == 1 and kinds.count("dispute_lost") == 1
    ddl(
        "UPDATE publications SET suspended_at = NULL, suspended_reason = NULL WHERE project_id = :p",
        p=project,
    )


async def test_every_suspension_path_records_one_notice_per_transaction(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, billing: Billing
) -> None:
    owner, ws, project, build, _app = await ready_build(client, idp, github)
    assert (await publish(owner, project, build_id=build)).status_code == 200
    ddl("UPDATE publications SET updated_at = now() WHERE project_id = :p", p=project)  # no suspension
    assert notices(ws) == []
    # One transaction suspending, lifting and suspending again merges into one notice (0034 trigger).
    with engine(MIGRATION_URL).begin() as conn:
        for reason in ("operator", None, "operator"):
            conn.execute(
                sa.text(
                    "UPDATE publications SET suspended_at = CASE WHEN CAST(:r AS text) IS NULL THEN NULL"
                    " ELSE now() END, suspended_reason = :r WHERE project_id = :p"
                ),
                {"r": reason, "p": project},
            )
    assert [(n.kind, n.details) for n in notices(ws)] == [
        ("site_suspended", {"reason": "operator", "project_ids": [project]})
    ]
    ddl(
        "UPDATE publications SET suspended_at = NULL, suspended_reason = NULL WHERE project_id = :p",
        p=project,
    )
    # The repository trigger of 0010 (GitHub revoked access) runs as another role; its notice is recorded too.
    repository = ddl("SELECT repository_id FROM projects WHERE id = :p", p=project)[0][0]
    ddl("UPDATE repositories SET access_revoked_at = now() WHERE id = :r", r=repository)
    assert notices(ws)[-1].details == {"reason": "repository_revoked", "project_ids": [project]}
    mailer = RecordingMailer()
    owner_email(ws)
    await delivery.deliver_due(billing.app.state.sessions, mailer, settings_of(billing))
    assert any("no longer has access to its repository" in m.body for m in mailer.sent)
    ddl("UPDATE repositories SET access_revoked_at = NULL WHERE id = :r", r=repository)


async def test_delivery_retries_transient_failures_and_gives_up_cleanly(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, billing: Billing
) -> None:
    _owner, ws, _project, _build, _app = await ready_build(client, idp, github)
    sessions = billing.app.state.sessions
    settings = settings_of(billing)
    address = owner_email(ws)
    ddl(
        "INSERT INTO owner_notices(workspace_id, kind, dedupe_key) VALUES (:w, 'dispute_opened', 'a')",
        w=ws,
    )
    mailer = RecordingMailer()
    mailer.fail = MailError("smtp_unavailable", transient=True)
    assert await delivery.deliver_due(sessions, mailer, settings) == {"pending": 1}
    row = notices(ws)[0]
    assert (row.state, row.attempts, row.last_error) == ("pending", 1, "smtp_unavailable")
    assert await delivery.deliver_due(sessions, mailer, settings) == {}  # waits for its retry time
    ddl("UPDATE owner_notices SET next_attempt_at = now() WHERE workspace_id = :w", w=ws)
    assert await delivery.deliver_due(sessions, mailer, settings) == {"sent": 1}
    assert notices(ws)[0].attempts == 2 and len(mailer.sent) == 1

    # A permanent failure fails the notice; one older than three days is never sent.
    ddl(
        "INSERT INTO owner_notices(workspace_id, kind, dedupe_key) VALUES (:w, 'dispute_lost', 'b')",
        w=ws,
    )
    mailer.fail = MailError("smtp_auth_failed", transient=False)
    assert await delivery.deliver_due(sessions, mailer, settings) == {"failed": 1}
    ddl(
        "INSERT INTO owner_notices(workspace_id, kind, dedupe_key, created_at)"
        " VALUES (:w, 'dispute_lost', 'c', now() - interval '4 days')",
        w=ws,
    )
    await delivery.deliver_due(sessions, mailer, settings)
    outcomes = {n.dedupe_key: (n.state, n.last_error) for n in notices(ws)}
    assert outcomes["b"] == ("failed", "smtp_auth_failed")
    assert outcomes["c"] == ("failed", "expired")
    # Without SMTP the notice is kept as skipped; refused recipients fail it without retrying.
    ddl(
        "INSERT INTO owner_notices(workspace_id, kind, dedupe_key) VALUES (:w, 'dispute_lost', 'd')",
        w=ws,
    )
    await delivery.deliver_due(sessions, None, settings)
    assert notices(ws)[-1].state == "skipped" and notices(ws)[-1].last_error == "email_not_configured"
    ddl(
        "INSERT INTO owner_notices(workspace_id, kind, dedupe_key) VALUES (:w, 'dispute_lost', 'e')",
        w=ws,
    )
    mailer.refuse = {address}
    await delivery.deliver_due(sessions, mailer, settings)
    assert (notices(ws)[-1].state, notices(ws)[-1].last_error) == ("failed", "recipients_refused")
    assert len(mailer.sent) == 1
    # A workspace being deleted is told nothing more.
    ddl(
        "INSERT INTO owner_notices(workspace_id, kind, dedupe_key) VALUES (:w, 'dispute_lost', 'f')",
        w=ws,
    )
    ddl("UPDATE workspaces SET status = 'deleting' WHERE id = :w", w=ws)
    try:
        await delivery.deliver_due(sessions, mailer, settings)
    finally:
        ddl("UPDATE workspaces SET status = 'active' WHERE id = :w", w=ws)
    assert (notices(ws)[-1].state, notices(ws)[-1].last_error) == ("skipped", "workspace_deleting")
