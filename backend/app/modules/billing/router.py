"""Billing routes (06 "Billing", 11): plan state, Checkout, Customer Portal, the trial, and Stripe webhooks.

The browser never chooses an amount: it names a plan key, and the price comes from operator settings.
Returning from Checkout grants nothing (A20); access changes only when a verified webhook (or the daily
reconciliation) has fetched the subscription from the provider. Only owners manage billing.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import exc, text

from app.adapters.billing.stripe import BillingError, SignatureError, StripeBilling, verify_webhook
from app.core import ratelimit
from app.core.errors import ApiError
from app.core.idempotency import run_once
from app.core.settings import Settings
from app.core.tenancy import set_context
from app.modules.billing import service
from app.modules.billing.plans import PAID, PLANS, TRIAL_DAYS, price_for
from app.modules.billing.sync import receipt_details
from app.modules.identity.sessions import Db
from app.modules.usage import ledger
from app.modules.workspaces.access import Member, ReadMember, WriteMember

logger = logging.getLogger(__name__)
router = APIRouter(tags=["billing"])
MAX_WEBHOOK_BYTES = 1024 * 1024


class PlanOut(BaseModel):
    key: str
    name: str
    allowance_micro_usd: int
    seats: int
    projects: int
    storage_gib: int
    active_builds: int
    available: bool  # a price is configured (paid plans) or the trial can still be started


class BillingOwner(BaseModel):
    kind: Literal["user", "organization"]
    handle: str


class BillingOut(BaseModel):
    configured: bool
    enforced: bool
    plan: str | None
    status: str | None
    period_end: datetime | None
    cancel_at_period_end: bool
    trial_ends_at: datetime | None
    trial_available: bool
    generation_blocked: str | None
    hosting_suspended: bool
    has_customer: bool
    plans: list[PlanOut]
    can_manage: bool = False  # 0040: the caller manages this plan (the person, or an organization owner)
    owner: BillingOwner | None = None  # whose plan this workspace uses


class CheckoutIn(BaseModel):
    plan: Literal["pro", "team"]


class RedirectOut(BaseModel):
    url: str


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _provider(request: Request) -> StripeBilling:
    provider: StripeBilling | None = request.app.state.billing
    settings = _settings(request)
    if provider is None or settings.stripe_webhook_secret is None:
        raise ApiError(503, "BILLING_UNAVAILABLE", "Billing is not configured on this deployment.")
    return provider


async def _manager(db: Db, member: Member) -> None:
    """Only the person (their own workspaces) or an owner of the organization manages its plan (0040)."""
    if member.support or not await service.manages_billing(db, member.user_id):
        raise ApiError(
            403,
            "FORBIDDEN",
            "Only the owner of this workspace's plan can change it: you, or an organization owner.",
        )


def _provider_error(error: BillingError) -> ApiError:
    logger.warning("billing provider call failed", extra={"reason": error.reason, "status": error.status})
    return ApiError(
        502, "BILLING_PROVIDER_ERROR", "The payment provider is unavailable. Try again.", retryable=True
    )


class PublicPlan(BaseModel):
    key: str
    name: str
    allowance_micro_usd: int
    seats: int
    projects: int
    storage_gib: int
    active_builds: int


class MetaOut(BaseModel):
    """Public facts the start, plans, legal and support pages show (02 S01, S23)."""

    operator_name: str
    support_email: str | None
    billing_available: bool
    trial_days: int
    plans: list[PublicPlan]


@router.get("/meta", response_model=MetaOut)
async def meta(request: Request) -> MetaOut:
    settings = _settings(request)
    configured = request.app.state.billing is not None and settings.stripe_webhook_secret is not None
    return MetaOut(
        operator_name=settings.operator_name,
        support_email=settings.support_email,
        billing_available=configured,
        trial_days=TRIAL_DAYS,
        plans=[
            PublicPlan(
                key=p.key,
                name=p.name,
                allowance_micro_usd=p.allowance_micro_usd,
                seats=p.seats,
                projects=p.projects,
                storage_gib=p.storage_gib,
                active_builds=p.active_builds,
            )
            for p in PLANS.values()
        ],
    )


@router.get("/workspaces/{workspace_id}/billing", response_model=BillingOut)
async def billing_state(request: Request, db: Db, member: ReadMember) -> BillingOut:
    settings = _settings(request)
    billing = await service.state(db, member.workspace_id, enforced=settings.billing_enforced)
    owner = await ledger.owner_of(db, member.workspace_id)
    has_customer = (
        await db.execute(
            text("SELECT EXISTS (SELECT 1 FROM billing_customers WHERE namespace_id = :n)"), {"n": owner}
        )
    ).scalar_one()
    can_manage = not member.support and await service.manages_billing(db, member.user_id)
    owner_row = (
        await db.execute(text("SELECT kind, handle FROM namespaces WHERE id = :n"), {"n": owner})
    ).one_or_none()
    await db.commit()
    configured = request.app.state.billing is not None and settings.stripe_webhook_secret is not None
    trial_available = not billing.trial_used and not billing.ever_subscribed
    plans = [
        PlanOut(
            key=plan.key,
            name=plan.name,
            allowance_micro_usd=plan.allowance_micro_usd,
            seats=plan.seats,
            projects=plan.projects,
            storage_gib=plan.storage_gib,
            active_builds=plan.active_builds,
            available=trial_available
            if plan.key == "trial"
            else configured and bool(price_for(settings, plan.key)),
        )
        for plan in PLANS.values()
    ]
    return BillingOut(
        configured=configured,
        enforced=settings.billing_enforced,
        plan=billing.plan.key if billing.plan else None,
        status=billing.status,
        period_end=billing.period_end,
        cancel_at_period_end=billing.cancel_at_period_end,
        trial_ends_at=billing.trial_ends_at,
        trial_available=trial_available,
        generation_blocked=billing.generation_blocked,
        hosting_suspended=billing.hosting_suspended,
        has_customer=bool(has_customer),
        plans=plans,
        can_manage=bool(can_manage),
        owner=BillingOwner(kind=owner_row.kind, handle=owner_row.handle) if owner_row else None,
    )


async def _customer(db: Db, provider: StripeBilling, workspace_id: UUID) -> tuple[str, UUID]:
    """The owner's provider customer and its anchor workspace (0040: one customer per owner, created once,
    through the workspace that first starts a checkout). The provider call runs with no transaction open;
    its idempotency key makes a concurrent second creation return the same customer."""
    owner = await ledger.owner_of(db, workspace_id)
    existing = (
        await db.execute(
            text("SELECT provider_customer_id, workspace_id FROM billing_customers WHERE namespace_id = :n"),
            {"n": owner},
        )
    ).one_or_none()
    await db.commit()
    if existing is not None:
        return str(existing.provider_customer_id), existing.workspace_id
    customer = await provider.create_customer(str(workspace_id), idempotency_key=f"customer:{owner}")
    await set_context(db, workspace_id=workspace_id)
    try:
        await db.execute(
            text("INSERT INTO billing_customers(workspace_id, provider_customer_id) VALUES (:w, :c)"),
            {"w": workspace_id, "c": customer},
        )
        await db.commit()
    except exc.IntegrityError:
        await db.rollback()  # a concurrent checkout of another of the owner's workspaces stored it first
    await set_context(db, workspace_id=workspace_id)
    stored = (
        await db.execute(
            text("SELECT provider_customer_id, workspace_id FROM billing_customers WHERE namespace_id = :n"),
            {"n": owner},
        )
    ).one()
    await db.commit()
    return str(stored.provider_customer_id), stored.workspace_id


@router.post("/workspaces/{workspace_id}/billing/checkout", response_model=RedirectOut)
async def checkout(
    body: CheckoutIn,
    request: Request,
    response: Response,
    db: Db,
    member: WriteMember,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> RedirectOut:
    await _manager(db, member)
    settings = _settings(request)
    provider = _provider(request)
    price = price_for(settings, body.plan)
    if body.plan not in PAID or price is None:
        raise ApiError(409, "PLAN_UNAVAILABLE", "This plan is not offered on this deployment.")

    async def produce() -> tuple[int, dict[str, Any]]:
        await ratelimit.hit(request.app.state.rate_sessions, settings, "billing", str(member.workspace_id))
        billing = await service.state(db, member.workspace_id)
        if billing.status in ("trialing", "active", "past_due", "unpaid"):
            raise ApiError(
                409,
                "ALREADY_SUBSCRIBED",
                "This workspace's owner already has a subscription. Manage it in the portal.",
            )
        try:
            customer, anchor = await _customer(db, provider, member.workspace_id)
            base = f"{settings.public_origin}/w/{member.workspace_id}/usage"
            url = await provider.create_checkout(
                customer=customer,
                price_id=price,
                workspace_id=str(anchor),  # the webhook names the workspace the customer belongs to
                success_url=f"{base}?checkout=returned",
                cancel_url=f"{base}?checkout=cancelled",
                idempotency_key=f"checkout:{member.workspace_id}:{idempotency_key}",
            )
        except BillingError as error:
            raise _provider_error(error) from error
        return 200, {"url": url}

    status, payload = await run_once(
        db,
        user_id=member.user_id,
        workspace_id=member.workspace_id,
        route=f"POST /workspaces/{member.workspace_id}/billing/checkout",
        key=idempotency_key,
        body=body.model_dump(mode="json"),
        produce=produce,
    )
    response.status_code = status
    return RedirectOut(**payload)


@router.post("/workspaces/{workspace_id}/billing/portal", response_model=RedirectOut)
async def portal(request: Request, db: Db, member: WriteMember) -> RedirectOut:
    await _manager(db, member)
    settings = _settings(request)
    provider = _provider(request)
    await ratelimit.hit(request.app.state.rate_sessions, settings, "billing", str(member.workspace_id))
    customer = (
        await db.execute(
            text("SELECT provider_customer_id FROM billing_customers WHERE namespace_id = :n"),
            {"n": await ledger.owner_of(db, member.workspace_id)},
        )
    ).scalar_one_or_none()
    await db.commit()
    if customer is None:
        raise ApiError(
            409, "NO_BILLING_ACCOUNT", "This workspace has no billing account yet. Choose a plan first."
        )
    try:
        url = await provider.create_portal(
            customer=str(customer), return_url=f"{settings.public_origin}/w/{member.workspace_id}/usage"
        )
    except BillingError as error:
        raise _provider_error(error) from error
    return RedirectOut(url=url)


@router.post("/workspaces/{workspace_id}/billing/trial", response_model=BillingOut)
async def start_trial(request: Request, db: Db, member: WriteMember) -> BillingOut:
    await _manager(db, member)
    granted = (
        await db.execute(
            text("SELECT app_private.start_trial(:w, :a, :d)"),
            {"w": member.workspace_id, "a": PLANS["trial"].allowance_micro_usd, "d": TRIAL_DAYS},
        )
    ).scalar_one()
    if granted is None:
        await db.rollback()
        raise ApiError(
            409,
            "TRIAL_UNAVAILABLE",
            "The trial is once per plan owner and per person, and never after a subscription.",
        )
    await db.commit()
    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)  # the commit cleared it
    return await billing_state(request, db, member)


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: Db) -> JSONResponse:
    """Verify, record and acknowledge; processing (provider fetch) runs in the worker (sync.process_pending)."""
    settings = _settings(request)
    if settings.stripe_webhook_secret is None:
        raise ApiError(503, "WEBHOOKS_DISABLED", "Webhooks are not configured.")
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", "Payload too large.")
    try:
        event = verify_webhook(
            body,
            request.headers.get("stripe-signature", ""),
            settings.stripe_webhook_secret.get_secret_value(),
        )
    except SignatureError as error:
        logger.warning("stripe webhook rejected", extra={"reason": str(error)})
        raise ApiError(400, "INVALID_SIGNATURE", "Signature verification failed.") from error
    if len(event["id"]) > 255 or len(event["type"]) > 128:
        raise ApiError(400, "BAD_REQUEST", "Malformed event.")
    details = receipt_details(event)
    receipt = (
        await db.execute(
            text(
                "INSERT INTO webhook_receipts(provider, provider_event_id, event_type, payload_digest, state, details,"
                " processed_at) VALUES ('stripe', :i, :t, :h, :s, CAST(:d AS jsonb),"
                " CASE WHEN :s = 'ignored' THEN now() END)"
                " ON CONFLICT (provider, provider_event_id) DO NOTHING RETURNING id"
            ),
            {
                "i": event["id"],
                "t": event["type"],
                "h": hashlib.sha256(body).hexdigest(),
                "s": "received" if details is not None else "ignored",
                "d": None if details is None else _dumps(details),
            },
        )
    ).scalar_one_or_none()
    await db.commit()
    return JSONResponse({"received": True, "duplicate": receipt is None})


def _dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True)
