"""Reconciling billing state with the payment provider (11 "Billing webhooks", A20).

A webhook is only a hint that a customer changed. Processing resolves the workspace from our own record
of the customer (never from event metadata), fetches every subscription of that customer from the
provider with no transaction open, then applies the snapshot in one transaction. Because the applied
state is always the provider's current state, duplicates and out-of-order events converge, and the
daily reconciliation repairs a dropped webhook the same way.

Event metadata that names a different workspace than the customer's is rejected and audited: it means
the event was crafted or a customer was attached to the wrong workspace, and neither may grant anything.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.billing.stripe import SubscriptionState
from app.core.settings import Settings
from app.core.tenancy import set_context
from app.modules.billing import service
from app.modules.billing.plans import plan_for_price

logger = logging.getLogger(__name__)
PROVIDER_ID = re.compile(r"^[A-Za-z0-9_]{1,255}$")
SYNC_EVENTS = frozenset(
    {
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "customer.subscription.paused",
        "customer.subscription.resumed",
        "invoice.paid",
        "invoice.payment_failed",
        "invoice.payment_action_required",
    }
)
DISPUTE_EVENTS = frozenset({"charge.dispute.created", "charge.dispute.closed"})
REFUND_EVENTS = frozenset({"charge.refunded"})
HANDLED = SYNC_EVENTS | DISPUTE_EVENTS | REFUND_EVENTS
BATCH = 50


class Provider(Protocol):
    async def subscriptions(self, customer: str) -> list[SubscriptionState]: ...

    async def charge_customer(self, charge_id: str) -> str | None: ...


def _id(value: Any) -> str | None:
    return value if isinstance(value, str) and PROVIDER_ID.match(value) else None


def receipt_details(event: dict[str, Any]) -> dict[str, Any] | None:
    """The few identifiers processing needs, taken from a verified event; None for unhandled types.
    No names, emails, addresses or card data are kept."""
    if event.get("type") not in HANDLED:
        return None
    obj = (event.get("data") or {}).get("object") or {}
    if not isinstance(obj, dict):
        return {}
    raw_metadata = obj.get("metadata")
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    claimed = metadata.get("workspace_id") or obj.get("client_reference_id")
    details: dict[str, Any] = {
        "customer": _id(obj.get("customer")),
        "claimed_workspace": claimed if isinstance(claimed, str) and len(claimed) <= 64 else None,
    }
    if event["type"] in DISPUTE_EVENTS:
        details["charge"] = _id(obj.get("charge"))
        details["dispute"] = _id(obj.get("id"))
        details["dispute_status"] = obj.get("status") if isinstance(obj.get("status"), str) else None
    if event["type"] in REFUND_EVENTS:
        details["charge"] = _id(obj.get("id"))
        refunded = obj.get("amount_refunded")
        details["amount_refunded"] = refunded if isinstance(refunded, int) else None
        details["currency"] = obj.get("currency") if isinstance(obj.get("currency"), str) else None
    return details


async def _audit(
    db: AsyncSession,
    workspace_id: UUID,
    action: str,
    metadata: dict[str, Any],
    resource_id: UUID | None = None,
) -> None:
    await db.execute(
        text(
            "INSERT INTO audit_events(workspace_id, action, resource_type, resource_id, metadata)"
            " VALUES (:w, :a, 'billing', :r, CAST(:m AS jsonb))"
        ),
        {"w": workspace_id, "a": action, "r": resource_id, "m": _json(metadata)},
    )


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, default=str, sort_keys=True)


async def _workspace(sessions: async_sessionmaker[AsyncSession], customer: str) -> UUID | None:
    async with sessions() as db:
        value = (
            await db.execute(text("SELECT app_private.billing_workspace(:c)"), {"c": customer})
        ).scalar_one()
        await db.commit()
    return value if value is None or isinstance(value, UUID) else UUID(str(value))


async def apply_subscriptions(
    db: AsyncSession,
    settings: Settings,
    workspace_id: UUID,
    customer: str,
    states: list[SubscriptionState],
    now: datetime | None = None,
) -> int:
    """Upsert the provider's subscriptions of one customer (caller set the workspace context and owns the
    transaction). Returns how many rows changed; each change is audited once."""
    now = now or datetime.now(UTC)
    changed = 0
    for sub in states:
        if sub.customer != customer or not _id(sub.id):
            continue  # the provider answered for another customer: never attach it here
        plan_key = plan_for_price(settings, sub.price_id)
        if plan_key is None:
            logger.warning("subscription with an unknown price", extra={"workspace_id": str(workspace_id)})
            continue
        previous = (
            await db.execute(
                text(
                    "SELECT id, status, plan_key, cancel_at_period_end, current_period_end, ended_at, past_due_since"
                    " FROM subscriptions WHERE workspace_id = :w AND provider_subscription_id = :s FOR UPDATE"
                ),
                {"w": workspace_id, "s": sub.id},
            )
        ).one_or_none()
        overdue = sub.status in ("past_due", "unpaid")
        past_due_since = (
            ((previous.past_due_since if previous is not None else None) or now) if overdue else None
        )
        values = {
            "w": workspace_id,
            "s": sub.id,
            "c": customer,
            "p": plan_key,
            "st": sub.status,
            "ps": sub.current_period_start,
            "pe": sub.current_period_end,
            "cancel": sub.cancel_at_period_end,
            "ended": sub.ended_at,
            "pd": past_due_since,
        }
        if previous is None:
            row_id = (
                await db.execute(
                    text(
                        "INSERT INTO subscriptions(workspace_id, provider_subscription_id, provider_customer_id,"
                        " plan_key, status, current_period_start, current_period_end, cancel_at_period_end,"
                        " ended_at, past_due_since) VALUES (:w, :s, :c, :p, :st, :ps, :pe, :cancel, :ended, :pd)"
                        " ON CONFLICT (provider_subscription_id) DO NOTHING RETURNING id"
                    ),
                    values,
                )
            ).scalar_one_or_none()
            if row_id is None:
                continue  # inserted concurrently (or owned elsewhere: RLS hides it); the next sync applies it
        else:
            row_id = previous.id
            same = (
                previous.status == sub.status
                and previous.plan_key == plan_key
                and previous.cancel_at_period_end == sub.cancel_at_period_end
                and previous.current_period_end == sub.current_period_end
                and previous.ended_at == sub.ended_at
            )
            await db.execute(
                text(
                    "UPDATE subscriptions SET plan_key = :p, status = :st, current_period_start = :ps,"
                    " current_period_end = :pe, cancel_at_period_end = :cancel, ended_at = :ended,"
                    " past_due_since = :pd, synced_at = now(),"
                    " updated_at = CASE WHEN :changed THEN now() ELSE updated_at END"
                    " WHERE workspace_id = :w AND provider_subscription_id = :s"
                ),
                {**values, "changed": not same},
            )
            if same:
                continue
        changed += 1
        await _audit(
            db,
            workspace_id,
            "billing.subscription_changed",
            {
                "status": sub.status,
                "previous_status": previous.status if previous is not None else None,
                "plan": plan_key,
                "cancel_at_period_end": sub.cancel_at_period_end,
            },
            row_id,
        )
    return changed


async def sync_customer(
    sessions: async_sessionmaker[AsyncSession],
    provider: Provider,
    settings: Settings,
    workspace_id: UUID,
    customer: str,
) -> int:
    states = await provider.subscriptions(customer)  # no transaction open (04)
    async with sessions() as db:
        await set_context(db, workspace_id=workspace_id)
        changed = await apply_subscriptions(db, settings, workspace_id, customer, states)
        await _hosting(db, workspace_id, enforced=settings.billing_enforced)
        await db.commit()
    return changed


async def _hosting(
    db: AsyncSession, workspace_id: UUID, now: datetime | None = None, *, enforced: bool
) -> int:
    """Suspend or lift billing suspensions of the workspace's publications from its billing state.
    Other suspension reasons (operator, connection, repository) are never touched."""
    billing = await service.state(db, workspace_id, now, enforced=enforced)
    if billing.hosting_suspended:
        result = await db.execute(
            text(
                "UPDATE publications SET suspended_at = now(), suspended_reason = 'billing', updated_at = now()"
                " WHERE workspace_id = :w AND suspended_at IS NULL"
            ),
            {"w": workspace_id},
        )
        action = "billing.hosting_suspended"
    else:
        result = await db.execute(
            text(
                "UPDATE publications SET suspended_at = NULL, suspended_reason = NULL, updated_at = now()"
                " WHERE workspace_id = :w AND suspended_reason = 'billing'"
            ),
            {"w": workspace_id},
        )
        action = "billing.hosting_restored"
    count = int(getattr(result, "rowcount", 0) or 0)
    if count:
        await _audit(db, workspace_id, action, {"publications": count})
    return count


async def _finish(
    sessions: async_sessionmaker[AsyncSession], receipt_id: UUID, state: str, workspace_id: UUID | None = None
) -> bool:
    """Mark a receipt done; False when another processor got there first."""
    async with sessions() as db:
        done = (
            await db.execute(
                text(
                    "UPDATE webhook_receipts SET state = :s, processed_at = now()"
                    " WHERE id = :id AND state = 'received' RETURNING id"
                ),
                {"s": state, "id": receipt_id},
            )
        ).scalar_one_or_none()
        await db.commit()
    return done is not None


async def process_receipt(
    sessions: async_sessionmaker[AsyncSession],
    provider: Provider,
    settings: Settings,
    receipt: Any,
) -> str:
    details: dict[str, Any] = receipt.details or {}
    event_type: str = receipt.event_type
    customer = _id(details.get("customer"))
    if event_type in DISPUTE_EVENTS and customer is None and _id(details.get("charge")):
        customer = _id(await provider.charge_customer(details["charge"]))
    workspace_id = await _workspace(sessions, customer) if customer is not None else None
    if workspace_id is None or customer is None:
        await _finish(sessions, receipt.id, "ignored")
        return "ignored"  # not one of our customers (another integration on the same account)
    claimed = details.get("claimed_workspace")
    if claimed and claimed != str(workspace_id):
        async with sessions() as db:
            await set_context(db, workspace_id=workspace_id)
            await _audit(db, workspace_id, "billing.webhook_rejected", {"event_type": event_type})
            await db.commit()
        await _finish(sessions, receipt.id, "rejected")
        logger.warning("stripe event metadata contradicts the customer", extra={"event_type": event_type})
        return "rejected"
    if event_type in DISPUTE_EVENTS:
        async with sessions() as db:
            await set_context(db, workspace_id=workspace_id)
            opened = event_type == "charge.dispute.created"
            await db.execute(
                text(
                    "UPDATE billing_customers SET disputed_at ="
                    " CASE WHEN :open THEN COALESCE(disputed_at, now()) ELSE NULL END WHERE workspace_id = :w"
                ),
                {"w": workspace_id, "open": opened},
            )
            await _audit(
                db,
                workspace_id,
                "billing.dispute_opened" if opened else "billing.dispute_closed",
                {"dispute": details.get("dispute"), "status": details.get("dispute_status")},
            )
            await db.commit()
    elif event_type in REFUND_EVENTS:
        # Money is the provider's record; a refund changes no allowance or ledger row, it is audited.
        async with sessions() as db:
            await set_context(db, workspace_id=workspace_id)
            await _audit(
                db,
                workspace_id,
                "billing.refunded",
                {
                    "charge": details.get("charge"),
                    "amount": details.get("amount_refunded"),
                    "currency": details.get("currency"),
                },
            )
            await db.commit()
    else:
        await sync_customer(sessions, provider, settings, workspace_id, customer)
    await _finish(sessions, receipt.id, "processed")
    return "processed"


async def process_pending(
    sessions: async_sessionmaker[AsyncSession], provider: Provider, settings: Settings
) -> dict[str, int]:
    """Process received Stripe receipts, oldest first. A failure leaves the receipt `received` for the next
    cycle (provider outage) unless it has been failing for a day, when it is marked `failed` and the daily
    reconciliation covers it."""
    async with sessions() as db:
        pending = (
            await db.execute(
                text(
                    "SELECT id, event_type, details, received_at FROM webhook_receipts"
                    " WHERE provider = 'stripe' AND state = 'received' ORDER BY received_at LIMIT :n"
                ),
                {"n": BATCH},
            )
        ).all()
        await db.commit()
    counts: dict[str, int] = {}
    for receipt in pending:
        try:
            outcome = await process_receipt(sessions, provider, settings, receipt)
        except Exception:
            logger.exception("stripe receipt processing failed", extra={"event_type": receipt.event_type})
            outcome = "retry"
            if receipt.received_at < datetime.now(UTC) - timedelta(days=1):
                await _finish(sessions, receipt.id, "failed")
                outcome = "failed"
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


async def reconcile(
    sessions: async_sessionmaker[AsyncSession], provider: Provider, settings: Settings
) -> dict[str, int]:
    """Re-sync every customer from the provider (daily, and `python -m app.ops.billing reconcile`):
    repairs dropped or failed webhooks and applies the hosting rules as time passes."""
    totals = {"customers": 0, "changed": 0, "failed": 0}
    after: UUID | None = None
    while True:
        async with sessions() as db:
            page = list(
                (
                    await db.execute(text("SELECT app_private.workspaces_after(:a, 100)"), {"a": after})
                ).scalars()
            )
            await db.commit()
        if not page:
            break
        for workspace_id in page:
            async with sessions() as db:
                await set_context(db, workspace_id=workspace_id)
                customer = (
                    await db.execute(
                        text("SELECT provider_customer_id FROM billing_customers WHERE workspace_id = :w"),
                        {"w": workspace_id},
                    )
                ).scalar_one_or_none()
                await db.commit()
            if customer is None:
                continue
            try:
                totals["changed"] += await sync_customer(sessions, provider, settings, workspace_id, customer)
                totals["customers"] += 1
            except Exception:
                totals["failed"] += 1
                logger.exception("billing reconciliation failed", extra={"workspace_id": str(workspace_id)})
        after = page[-1]
    return totals


async def sweep_hosting(
    sessions: async_sessionmaker[AsyncSession], *, enforced: bool, now: datetime | None = None
) -> int:
    """Apply the time-based hosting rules (past due beyond grace, ended beyond the read window) without
    calling the provider; runs with the maintenance loop."""
    changed = 0
    after: UUID | None = None
    while True:
        async with sessions() as db:
            page = list(
                (
                    await db.execute(text("SELECT app_private.workspaces_after(:a, 100)"), {"a": after})
                ).scalars()
            )
            await db.commit()
        if not page:
            break
        for workspace_id in page:
            async with sessions() as db:
                await set_context(db, workspace_id=workspace_id)
                has = (
                    await db.execute(
                        text("SELECT EXISTS (SELECT 1 FROM subscriptions WHERE workspace_id = :w)"),
                        {"w": workspace_id},
                    )
                ).scalar_one()
                if has:
                    changed += await _hosting(db, workspace_id, now, enforced=enforced)
                await db.commit()
        after = page[-1]
    return changed
