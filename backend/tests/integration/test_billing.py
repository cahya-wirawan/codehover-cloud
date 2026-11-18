"""Billing (M5; 11 "Monetization and billing", 06 "Billing", A20): the payment failure matrix.

Stripe is replayed by `FakeStripe` through `httpx.MockTransport`: it keeps customers, subscriptions and
charges the way the provider would, and signs webhook events with the documented scheme. Tests run the
worker's processing step (`sync.process_pending`) directly, as the maintenance loop would.
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.billing.stripe import StripeBilling, sign
from app.modules.billing import sync
from tests.integration.auth_support import FakeIdP, ddl, unsafe
from tests.integration.conftest import needs_db
from tests.integration.test_budgets import make_estimate, start_from
from tests.integration.test_builds import drive, setup_project
from tests.integration.test_generation import runtime_for
from tests.integration.test_github import FakeGitHub, github, workspace  # noqa: F401
from tests.integration.test_publication import publish, ready_build
from tests.integration.test_workspaces import NewUser, code, join, new_user  # noqa: F401

pytestmark = [pytest.mark.integration, needs_db]
WEBHOOK_SECRET = "whsec_test_only"
PRICES = {"pro": "price_pro_test", "team": "price_team_test"}


class FakeStripe:
    """Customers, subscriptions and charges as the provider holds them; the source of truth (11)."""

    def __init__(self) -> None:
        self.customers: dict[str, dict[str, Any]] = {}
        self.by_key: dict[str, str] = {}
        self.subs: dict[str, dict[str, Any]] = {}
        self.charges: dict[str, str] = {}
        self.checkouts: list[dict[str, str]] = []
        self.calls: list[str] = []
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.method} {request.url.path}")
        assert request.headers["authorization"] == "Bearer sk_test_only"
        if self.down:
            return httpx.Response(503, json={"error": {"message": "stripe-internal-detail"}})
        path = request.url.path.removeprefix("/v1")
        form = dict(parse_qsl(request.content.decode())) if request.method == "POST" else {}
        if path == "/customers" and request.method == "POST":
            key = request.headers.get("idempotency-key", secrets.token_hex(8))
            if key not in self.by_key:
                customer = f"cus_{secrets.token_hex(6)}"
                self.customers[customer] = {"workspace": form.get("metadata[workspace_id]")}
                self.by_key[key] = customer
            return httpx.Response(200, json={"id": self.by_key[key], "object": "customer"})
        if path == "/checkout/sessions":
            self.checkouts.append(form)
            return httpx.Response(
                200, json={"id": "cs_test", "url": f"https://checkout.stripe.test/{len(self.checkouts)}"}
            )
        if path == "/billing_portal/sessions":
            return httpx.Response(200, json={"url": f"https://billing.stripe.test/p/{form['customer']}"})
        if path == "/subscriptions" and request.method == "GET":
            customer = request.url.params["customer"]
            data = [s for s in self.subs.values() if s["customer"] == customer]
            return httpx.Response(200, json={"object": "list", "data": data, "has_more": False})
        if path.startswith("/charges/"):
            charge = path.removeprefix("/charges/")
            if charge not in self.charges:
                return httpx.Response(404, json={"error": {"message": "no such charge"}})
            return httpx.Response(200, json={"id": charge, "customer": self.charges[charge]})
        return httpx.Response(404, json={"error": {"message": f"unexpected {path}"}})

    def subscribe(self, customer: str, plan: str = "pro", status: str = "active", **extra: Any) -> str:
        sub = f"sub_{secrets.token_hex(6)}"
        now = int(time.time())
        self.subs[sub] = {
            "id": sub,
            "customer": customer,
            "status": status,
            "cancel_at_period_end": False,
            "ended_at": None,
            "items": {
                "data": [
                    {
                        "price": {"id": PRICES[plan]},
                        "current_period_start": now,
                        "current_period_end": now + 30 * 86400,
                    }
                ]
            },
            **extra,
        }
        return sub

    def update(self, sub: str, **fields: Any) -> None:
        self.subs[sub].update(fields)


def event(kind: str, obj: dict[str, Any], event_id: str | None = None) -> bytes:
    return json.dumps(
        {"id": event_id or f"evt_{secrets.token_hex(8)}", "type": kind, "data": {"object": obj}}
    ).encode()


async def deliver(
    client: httpx.AsyncClient, body: bytes, secret: str = WEBHOOK_SECRET, **kw: Any
) -> httpx.Response:
    return await client.post(
        "/api/v1/webhooks/stripe",
        content=body,
        headers={"Stripe-Signature": sign(body, secret, **kw), "Content-Type": "application/json"},
    )


class Billing:
    def __init__(self, client: httpx.AsyncClient, fake: FakeStripe) -> None:
        self.client = client
        self.app = client.app  # type: ignore[attr-defined]
        self.fake = fake
        self.provider = StripeBilling(
            "sk_test_only", httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
        )

    def configure(self, **settings: Any) -> None:
        self.app.state.settings = self.app.state.settings.model_copy(
            update={
                "stripe_secret_key": SecretStr("sk_test_only"),
                "stripe_webhook_secret": SecretStr(WEBHOOK_SECRET),
                "stripe_price_pro": PRICES["pro"],
                "stripe_price_team": PRICES["team"],
                **settings,
            }
        )
        self.app.state.billing = self.provider

    async def process(self) -> dict[str, int]:
        return await sync.process_pending(self.app.state.sessions, self.provider, self.app.state.settings)

    async def reconcile(self) -> dict[str, int]:
        return await sync.reconcile(self.app.state.sessions, self.provider, self.app.state.settings)

    def customer_for(self, ws: str) -> str:
        """Attach a provider customer to a workspace the way checkout does."""
        customer = f"cus_{secrets.token_hex(6)}"
        self.fake.customers[customer] = {"workspace": ws}
        ddl(
            "INSERT INTO billing_customers(workspace_id, provider_customer_id) VALUES (:w, :c)",
            w=ws,
            c=customer,
        )
        return customer


@pytest.fixture
def billing(client: httpx.AsyncClient) -> Billing:
    setup = Billing(client, FakeStripe())
    setup.configure()
    return setup


async def state(owner: httpx.AsyncClient, ws: str) -> dict[str, Any]:
    response = await owner.get(f"/api/v1/workspaces/{ws}/billing")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def checkout(owner: httpx.AsyncClient, ws: str, plan: str = "pro") -> httpx.Response:
    return await unsafe(
        owner,
        "POST",
        f"/api/v1/workspaces/{ws}/billing/checkout",
        json={"plan": plan},
        headers={"Idempotency-Key": secrets.token_hex(12)},
    )


def receipt_state(event_id: str) -> str:
    return str(
        ddl(
            "SELECT state FROM webhook_receipts WHERE provider = 'stripe' AND provider_event_id = :e",
            e=event_id,
        )[0][0]
    )


def suspension(project: str) -> str | None:
    reason = ddl("SELECT suspended_reason FROM publications WHERE project_id = :p", p=project)[0][0]
    return None if reason is None else str(reason)


def audit_actions(ws: str) -> list[str]:
    return [
        r[0]
        for r in ddl(
            "SELECT action FROM audit_events WHERE workspace_id = :w AND action LIKE 'billing.%'"
            " ORDER BY created_at",
            w=ws,
        )
    ]


# ---------------------------------------------------------------- webhook authenticity
async def test_forged_stale_malformed_and_duplicate_webhooks(
    client: httpx.AsyncClient, billing: Billing
) -> None:
    body = event("invoice.paid", {"customer": "cus_unknown"}, "evt_" + secrets.token_hex(8))
    assert (await deliver(client, body, secret="whsec_wrong")).status_code == 400  # forged
    stale = await deliver(client, body, timestamp=int(time.time()) - 3600)
    assert stale.status_code == 400 and stale.json()["error"]["code"] == "INVALID_SIGNATURE"
    unsigned = await client.post("/api/v1/webhooks/stripe", content=body)
    assert unsigned.status_code == 400
    tampered = body.replace(b"cus_unknown", b"cus_other")
    header = sign(body, WEBHOOK_SECRET)
    assert (
        await client.post("/api/v1/webhooks/stripe", content=tampered, headers={"Stripe-Signature": header})
    ).status_code == 400
    assert (
        ddl(
            "SELECT count(*) FROM webhook_receipts WHERE provider = 'stripe' AND provider_event_id = :e",
            e=json.loads(body)["id"],
        )[0][0]
        == 0
    )  # nothing unverified is recorded

    first = await deliver(client, body)
    again = await deliver(client, body)
    assert first.status_code == 200 and first.json()["duplicate"] is False
    assert again.status_code == 200 and again.json()["duplicate"] is True
    event_id = json.loads(body)["id"]
    await billing.process()
    assert receipt_state(event_id) == "ignored"  # not one of our customers

    unhandled = event("customer.created", {"id": "cus_x", "email": "someone@example.test"})
    assert (await deliver(client, unhandled)).status_code == 200
    row = ddl(
        "SELECT state, details FROM webhook_receipts WHERE provider_event_id = :e",
        e=json.loads(unhandled)["id"],
    )[0]
    assert row[0] == "ignored" and row[1] is None  # no customer details kept for unhandled events

    billing.app.state.settings = billing.app.state.settings.model_copy(update={"stripe_webhook_secret": None})
    assert (await deliver(client, body)).status_code == 503


# ---------------------------------------------------------------- checkout (A20)
async def test_returning_from_checkout_grants_nothing_until_the_provider_confirms(
    client: httpx.AsyncClient, billing: Billing, new_user: NewUser
) -> None:
    owner = await new_user()
    ws = await workspace(owner)
    before = await state(owner, ws)
    assert before["configured"] is True and before["plan"] is None and before["trial_available"] is True
    assert {p["key"]: p["available"] for p in before["plans"]} == {"trial": True, "pro": True, "team": True}
    usage_before = (await owner.get(f"/api/v1/workspaces/{ws}/usage")).json()["allowance_micro_usd"]

    assert (
        await unsafe(
            owner,
            "POST",
            f"/api/v1/workspaces/{ws}/billing/checkout",
            json={"plan": "enterprise"},
            headers={"Idempotency-Key": secrets.token_hex(12)},
        )
    ).status_code == 422
    started = await checkout(owner, ws)
    assert started.status_code == 200, started.text
    assert started.json()["url"].startswith("https://checkout.stripe.test/")
    sent = billing.fake.checkouts[-1]
    assert sent["line_items[0][price]"] == PRICES["pro"] and sent["client_reference_id"] == ws
    assert sent["success_url"].endswith(f"/w/{ws}/usage?checkout=returned")
    customer = sent["customer"]
    assert (
        ddl("SELECT provider_customer_id FROM billing_customers WHERE workspace_id = :w", w=ws)[0][0]
        == customer
    )

    # The browser comes back from Checkout and the provider has the subscription, but no webhook yet.
    sub = billing.fake.subscribe(customer)
    returned = await state(owner, ws)
    assert returned["plan"] is None and returned["status"] is None  # the redirect proves nothing
    assert (await owner.get(f"/api/v1/workspaces/{ws}/usage")).json()["allowance_micro_usd"] == usage_before

    body = event(
        "checkout.session.completed",
        {"customer": customer, "client_reference_id": ws, "metadata": {"workspace_id": ws}},
    )
    assert (await deliver(client, body)).status_code == 200
    assert (await billing.process()).get("processed") == 1
    after = await state(owner, ws)
    assert after["plan"] == "pro" and after["status"] == "active" and after["trial_available"] is False
    assert (await owner.get(f"/api/v1/workspaces/{ws}/usage")).json()["allowance_micro_usd"] == 5_000_000
    assert ddl("SELECT plan_key, status FROM subscriptions WHERE provider_subscription_id = :s", s=sub)[
        0
    ] == ("pro", "active")
    assert audit_actions(ws) == ["billing.subscription_changed"]

    again = await checkout(owner, ws, "team")
    assert again.status_code == 409 and code(again) == "ALREADY_SUBSCRIBED"
    portal = await unsafe(owner, "POST", f"/api/v1/workspaces/{ws}/billing/portal")
    assert portal.status_code == 200 and portal.json()["url"].endswith(customer)


async def test_only_owners_manage_billing_and_it_needs_configuration(
    client: httpx.AsyncClient, billing: Billing, new_user: NewUser
) -> None:
    owner = await new_user()
    ws = await workspace(owner)
    admin = await new_user()
    await join(owner, ws, admin, "admin")
    assert (await state(admin, ws))["plan"] is None  # members can see the plan
    for path in ("checkout", "portal", "trial"):
        kwargs: dict[str, Any] = {"json": {"plan": "pro"}} if path == "checkout" else {}
        response = await unsafe(
            admin,
            "POST",
            f"/api/v1/workspaces/{ws}/billing/{path}",
            headers={"Idempotency-Key": secrets.token_hex(12)},
            **kwargs,
        )
        assert response.status_code == 403, (path, response.text)
    no_account = await unsafe(owner, "POST", f"/api/v1/workspaces/{ws}/billing/portal")
    assert no_account.status_code == 409 and code(no_account) == "NO_BILLING_ACCOUNT"

    billing.fake.down = True
    failed = await checkout(owner, ws)
    assert failed.status_code == 502 and failed.json()["error"]["retryable"] is True
    assert "stripe-internal-detail" not in failed.text  # provider text never reaches the client
    billing.fake.down = False

    billing.app.state.billing = None
    unconfigured = await checkout(owner, ws)
    assert unconfigured.status_code == 503 and code(unconfigured) == "BILLING_UNAVAILABLE"
    assert (await state(owner, ws))["configured"] is False


# ---------------------------------------------------------------- order, duplicates, reconciliation
async def test_out_of_order_and_repeated_events_converge_on_the_provider_state(
    client: httpx.AsyncClient, billing: Billing, new_user: NewUser
) -> None:
    owner = await new_user()
    ws = await workspace(owner)
    customer = billing.customer_for(ws)
    sub = billing.fake.subscribe(customer)
    created = event("customer.subscription.created", {"id": sub, "customer": customer, "status": "active"})
    billing.fake.update(sub, status="canceled", ended_at=int(time.time()))
    deleted = event("customer.subscription.deleted", {"id": sub, "customer": customer, "status": "canceled"})
    # The deletion arrives (and is processed) before the creation, then the creation is redelivered.
    assert (await deliver(client, deleted)).status_code == 200
    await billing.process()
    assert (await deliver(client, created)).status_code == 200
    assert (await deliver(client, created)).json()["duplicate"] is True
    await billing.process()
    current = await state(owner, ws)
    assert current["plan"] is None and current["status"] == "canceled"
    assert audit_actions(ws) == ["billing.subscription_changed"]  # the stale event changed nothing


async def test_reconciliation_repairs_a_dropped_webhook(
    client: httpx.AsyncClient, billing: Billing, new_user: NewUser
) -> None:
    owner = await new_user()
    ws = await workspace(owner)
    customer = billing.customer_for(ws)
    billing.fake.subscribe(customer, plan="team")
    assert (await state(owner, ws))["plan"] is None  # the webhook never arrived
    billing.fake.down = True
    failed = await billing.reconcile()
    assert failed["failed"] >= 1  # a provider outage changes nothing and is reported
    assert (await state(owner, ws))["plan"] is None
    billing.fake.down = False
    totals = await billing.reconcile()
    assert totals["changed"] >= 1
    assert (await state(owner, ws))["plan"] == "team"


async def test_a_provider_outage_leaves_receipts_for_the_next_cycle(
    client: httpx.AsyncClient, billing: Billing, new_user: NewUser
) -> None:
    owner = await new_user()
    ws = await workspace(owner)
    customer = billing.customer_for(ws)
    billing.fake.subscribe(customer)
    body = event("invoice.paid", {"customer": customer})
    await deliver(client, body)
    billing.fake.down = True
    assert (await billing.process()).get("retry", 0) >= 1
    assert receipt_state(json.loads(body)["id"]) == "received"
    billing.fake.down = False
    await billing.process()
    assert receipt_state(json.loads(body)["id"]) == "processed"
    assert (await state(owner, ws))["plan"] == "pro"


async def test_metadata_naming_another_workspace_is_rejected(
    client: httpx.AsyncClient, billing: Billing, new_user: NewUser
) -> None:
    victim, attacker = await new_user(), await new_user()
    victim_ws, attacker_ws = await workspace(victim), await workspace(attacker)
    customer = billing.customer_for(attacker_ws)
    billing.fake.subscribe(customer)
    body = event(
        "checkout.session.completed", {"customer": customer, "metadata": {"workspace_id": victim_ws}}
    )
    await deliver(client, body)
    await billing.process()
    assert receipt_state(json.loads(body)["id"]) == "rejected"
    assert (await state(victim, victim_ws))["plan"] is None
    assert (await state(attacker, attacker_ws))["plan"] is None  # a rejected event grants nothing at all
    assert audit_actions(attacker_ws) == ["billing.webhook_rejected"]
    assert audit_actions(victim_ws) == []


# ---------------------------------------------------------------- lifecycle
async def test_a_failed_payment_blocks_generation_until_it_recovers(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, billing: Billing
) -> None:
    owner, ws, project, build, app = await ready_build(client, idp, github)
    customer = billing.customer_for(ws)
    sub = billing.fake.subscribe(customer)
    await deliver(client, event("invoice.paid", {"customer": customer}))
    await billing.process()
    snapshot = str(ddl("SELECT snapshot_id FROM builds WHERE id = :b", b=build)[0][0])

    billing.fake.update(sub, status="past_due")
    await deliver(client, event("invoice.payment_failed", {"customer": customer}))
    await billing.process()
    overdue = await state(owner, ws)
    assert overdue["plan"] == "pro" and overdue["generation_blocked"] == "past_due"
    assert overdue["hosting_suspended"] is False  # inside the grace period
    estimate = (await make_estimate(owner, project, snapshot, preset="replay-v2", guide_pages=3)).json()
    blocked = await start_from(owner, project, estimate["id"])
    assert blocked.status_code == 402 and code(blocked) == "BILLING_PAST_DUE"
    assert (await owner.get(f"/api/v1/builds/{build}/tree")).status_code == 200  # reading continues

    billing.fake.update(sub, status="active")
    await deliver(client, event("invoice.paid", {"customer": customer}))
    await billing.process()
    assert (await state(owner, ws))["generation_blocked"] is None
    assert (
        ddl("SELECT past_due_since FROM subscriptions WHERE provider_subscription_id = :s", s=sub)[0][0]
        is None
    )
    started = await start_from(owner, project, estimate["id"])
    assert started.status_code == 202, started.text
    await drive(app, started.json()["id"], runtime=runtime_for(app))


async def test_past_due_beyond_grace_and_ended_beyond_the_read_window_suspend_hosting(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, billing: Billing
) -> None:
    owner, ws, project, build, _app = await ready_build(client, idp, github)
    assert (await publish(owner, project, build_id=build)).status_code == 200
    customer = billing.customer_for(ws)
    sub = billing.fake.subscribe(customer, status="past_due")
    await deliver(client, event("invoice.payment_failed", {"customer": customer}))
    await billing.process()
    sessions = billing.app.state.sessions
    await sync.sweep_hosting(sessions, enforced=False)
    assert suspension(project) is None  # inside the grace period
    ddl(
        "UPDATE subscriptions SET past_due_since = now() - interval '8 days' WHERE provider_subscription_id = :s",
        s=sub,
    )
    assert await sync.sweep_hosting(sessions, enforced=False) >= 1  # overdue: in the beta too
    assert (
        ddl("SELECT suspended_reason FROM publications WHERE project_id = :p", p=project)[0][0] == "billing"
    )
    assert (await owner.get(f"/api/v1/builds/{build}/tree")).status_code == 200  # members still read

    billing.fake.update(sub, status="active")
    await deliver(client, event("invoice.paid", {"customer": customer}))
    await billing.process()
    assert ddl("SELECT suspended_at FROM publications WHERE project_id = :p", p=project)[0][0] is None

    # Cancel at period end: everything continues while the provider keeps the subscription active.
    billing.fake.update(sub, cancel_at_period_end=True)
    await deliver(client, event("customer.subscription.updated", {"id": sub, "customer": customer}))
    await billing.process()
    ending = await state(owner, ws)
    assert ending["plan"] == "pro" and ending["cancel_at_period_end"] is True

    ended_at = int((datetime.now(UTC) - timedelta(days=10)).timestamp())
    billing.fake.update(sub, status="canceled", ended_at=ended_at)
    await deliver(client, event("customer.subscription.deleted", {"id": sub, "customer": customer}))
    await billing.process()
    ended = await state(owner, ws)
    assert ended["plan"] is None and ended["hosting_suspended"] is False  # read and export for 30 days
    ddl(
        "UPDATE subscriptions SET ended_at = now() - interval '31 days' WHERE provider_subscription_id = :s",
        s=sub,
    )
    # In the beta a workspace without a plan keeps the defaults: an ended subscription takes nothing offline.
    await sync.sweep_hosting(sessions, enforced=False)
    assert suspension(project) is None
    assert (await state(owner, ws))["hosting_suspended"] is False
    billing.configure(billing_enforced=True)
    assert await sync.sweep_hosting(sessions, enforced=True) >= 1
    assert (await state(owner, ws))["hosting_suspended"] is True
    assert (
        ddl("SELECT suspended_reason FROM publications WHERE project_id = :p", p=project)[0][0] == "billing"
    )
    assert "billing.hosting_suspended" in audit_actions(ws)
    assert ddl("SELECT count(*) FROM builds WHERE id = :b AND status = 'ready'", b=build)[0][0] == 1  # kept

    # An operator takedown is never lifted by billing.
    ddl("UPDATE publications SET suspended_reason = 'operator' WHERE project_id = :p", p=project)
    billing.fake.subscribe(customer)
    await billing.reconcile()
    assert (
        ddl("SELECT suspended_reason FROM publications WHERE project_id = :p", p=project)[0][0] == "operator"
    )
    ddl(
        "UPDATE publications SET suspended_at = NULL, suspended_reason = NULL WHERE project_id = :p",
        p=project,
    )


async def test_disputes_block_generation_and_refunds_are_audited(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, billing: Billing
) -> None:
    owner, ws, project, build, _app = await ready_build(client, idp, github)
    customer = billing.customer_for(ws)
    billing.fake.subscribe(customer)
    await billing.reconcile()
    charge = "ch_" + ws.replace("-", "")[:12]
    billing.fake.charges[charge] = customer
    await deliver(
        client, event("charge.dispute.created", {"id": "dp_1", "charge": charge, "status": "needs_response"})
    )
    await billing.process()
    disputed = await state(owner, ws)
    assert disputed["plan"] == "pro" and disputed["generation_blocked"] == "disputed"
    snapshot = str(ddl("SELECT snapshot_id FROM builds WHERE id = :b", b=build)[0][0])
    estimate = (await make_estimate(owner, project, snapshot, preset="replay-v2", guide_pages=3)).json()
    blocked = await start_from(owner, project, estimate["id"])
    assert blocked.status_code == 402 and code(blocked) == "BILLING_DISPUTED"

    await deliver(client, event("charge.dispute.closed", {"id": "dp_1", "charge": charge, "status": "won"}))
    await billing.process()
    assert (await state(owner, ws))["generation_blocked"] is None

    ledger_before = ddl("SELECT count(*) FROM usage_entries WHERE workspace_id = :w", w=ws)[0][0]
    await deliver(
        client,
        event(
            "charge.refunded",
            {
                "id": charge,
                "customer": customer,
                "amount_refunded": 1200,
                "currency": "usd",
                "billing_details": {"email": "x@example.test"},
            },
        ),
    )
    await billing.process()
    assert ddl("SELECT count(*) FROM usage_entries WHERE workspace_id = :w", w=ws)[0][0] == ledger_before
    refund = ddl(
        "SELECT metadata FROM audit_events WHERE workspace_id = :w AND action = 'billing.refunded'", w=ws
    )
    assert refund[0][0] == {"amount": 1200, "charge": charge, "currency": "usd"}
    assert "example.test" not in json.dumps(
        ddl(
            "SELECT details FROM webhook_receipts WHERE event_type = 'charge.refunded' AND details->>'charge' = :c",
            c=charge,
        )[0][0]
    )
    assert audit_actions(ws)[-3:] == ["billing.dispute_opened", "billing.dispute_closed", "billing.refunded"]


# ---------------------------------------------------------------- trial and plan limits
async def test_the_trial_is_once_per_workspace_and_counted_once(
    client: httpx.AsyncClient, billing: Billing, new_user: NewUser
) -> None:
    owner = await new_user()
    ws = await workspace(owner)
    base = (await owner.get(f"/api/v1/workspaces/{ws}/usage")).json()["allowance_micro_usd"]
    started = await unsafe(owner, "POST", f"/api/v1/workspaces/{ws}/billing/trial")
    assert started.status_code == 200, started.text
    assert started.json()["plan"] == "trial" and started.json()["trial_available"] is False
    assert (await owner.get(f"/api/v1/workspaces/{ws}/usage")).json()[
        "allowance_micro_usd"
    ] == base + 2_000_000
    again = await unsafe(owner, "POST", f"/api/v1/workspaces/{ws}/billing/trial")
    assert again.status_code == 409 and code(again) == "TRIAL_UNAVAILABLE"
    assert audit_actions(ws) == ["billing.trial_started"]
    # A one-time grant counts only in the month it starts, even while it has not expired.
    ddl("UPDATE entitlement_grants SET starts_at = now() - interval '40 days' WHERE workspace_id = :w", w=ws)
    assert (await owner.get(f"/api/v1/workspaces/{ws}/usage")).json()["allowance_micro_usd"] == base

    subscribed = await new_user()
    other = await workspace(subscribed)
    billing.fake.subscribe(billing.customer_for(other), status="canceled", ended_at=int(time.time()))
    await billing.reconcile()
    assert (await state(subscribed, other))["trial_available"] is False
    refused = await unsafe(subscribed, "POST", f"/api/v1/workspaces/{other}/billing/trial")
    assert refused.status_code == 409  # never a trial after a subscription


async def test_plan_limits_apply_to_new_projects_and_seats_when_enforced(
    client: httpx.AsyncClient, idp: FakeIdP, github: FakeGitHub, billing: Billing, new_user: NewUser
) -> None:
    owner, ws, project = await setup_project(client, idp, github)
    billing.configure(billing_enforced=True)
    repo = str(ddl("SELECT repository_id FROM projects WHERE id = :p", p=project)[0][0])

    async def create(slug: str) -> httpx.Response:
        return await unsafe(
            owner,
            "POST",
            f"/api/v1/workspaces/{ws}/projects",
            json={"repository_id": repo, "name": slug, "slug": slug},
        )

    no_plan = await create("second")
    assert no_plan.status_code == 402 and code(no_plan) == "PLAN_REQUIRED"
    assert (await owner.get(f"/api/v1/projects/{project}")).status_code == 200  # reading is unaffected
    assert (await unsafe(owner, "POST", f"/api/v1/workspaces/{ws}/billing/trial")).status_code == 200
    full = await create("second")
    assert full.status_code == 409 and code(full) == "PLAN_LIMIT"
    assert full.json()["error"]["details"] == {"limit": 1, "kind": "projects"}
    seat = await unsafe(
        owner,
        "POST",
        f"/api/v1/workspaces/{ws}/invitations",
        json={"email": "new@example.test", "role": "viewer"},
    )
    assert seat.status_code == 409 and code(seat) == "PLAN_LIMIT"

    customer = billing.customer_for(ws)
    billing.fake.subscribe(customer, plan="team")
    await billing.reconcile()
    assert (await create("second")).status_code == 201
    assert (
        await unsafe(
            owner,
            "POST",
            f"/api/v1/workspaces/{ws}/invitations",
            json={"email": "new@example.test", "role": "viewer"},
        )
    ).status_code == 201


async def test_a_subscribed_workspace_cannot_be_deleted_until_cancelled(
    client: httpx.AsyncClient, billing: Billing, new_user: NewUser
) -> None:
    owner = await new_user()
    ws = await workspace(owner)
    slug = (await owner.get(f"/api/v1/workspaces/{ws}")).json()["slug"]
    customer = billing.customer_for(ws)
    sub = billing.fake.subscribe(customer)
    await billing.reconcile()
    refused = await unsafe(owner, "POST", f"/api/v1/workspaces/{ws}/deletion", json={"confirmation": slug})
    assert refused.status_code == 409 and code(refused) == "SUBSCRIPTION_ACTIVE"
    billing.fake.update(sub, status="canceled", ended_at=int(time.time()))
    await billing.reconcile()
    deleted = await unsafe(owner, "POST", f"/api/v1/workspaces/{ws}/deletion", json={"confirmation": slug})
    assert deleted.status_code == 202, deleted.text


async def test_meta_is_public_and_names_the_operator(client: httpx.AsyncClient, billing: Billing) -> None:
    billing.configure(operator_name="Example Operator", support_email="help@example.test")
    response = await client.get("/api/v1/meta")
    assert response.status_code == 200
    body = response.json()
    assert body["operator_name"] == "Example Operator" and body["support_email"] == "help@example.test"
    assert body["billing_available"] is True and body["trial_days"] == 14
    assert [p["key"] for p in body["plans"]] == ["trial", "pro", "team"]
