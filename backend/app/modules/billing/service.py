"""A workspace's billing state and the rules it implies (11 "State policy").

- trialing/active: the plan's allowance and limits apply.
- past_due (or unpaid): the plan's limits remain, new generation is blocked; reading continues for
  PAST_DUE_GRACE_DAYS, then hosting is suspended.
- cancel at period end: everything continues until the period ends (the provider keeps it `active`).
- ended (cancelled): read and export continue for ENDED_READ_DAYS, then hosting is suspended when plans are
  enforced. Nothing is deleted.
- an open payment dispute blocks new generation until it is won.
- a trial (one per workspace, never after a subscription) is the Trial plan for TRIAL_DAYS.

With `BILLING_ENFORCED` off (the beta), a workspace without a plan keeps the operator defaults; with it
on, creating projects and inviting members needs a plan. Reading never depends on the plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.settings import Settings
from app.modules.billing.plans import (
    ENDED_READ_DAYS,
    HOLDS_PLAN,
    PAST_DUE_GRACE_DAYS,
    PLANS,
    Plan,
)


@dataclass(frozen=True)
class BillingState:
    plan: Plan | None
    status: str | None  # the provider status of the current subscription, "trial", or None
    subscription_id: str | None
    period_end: datetime | None
    cancel_at_period_end: bool
    trial_ends_at: datetime | None
    trial_used: bool
    generation_blocked: str | None  # "past_due" | "disputed" | None
    hosting_suspended: bool
    ever_subscribed: bool


async def state(
    db: AsyncSession, workspace_id: UUID, now: datetime | None = None, *, enforced: bool = False
) -> BillingState:
    """Read in the caller's workspace context. `enforced` is `BILLING_ENFORCED`: only then does an ended
    subscription take sites offline (in the beta a workspace without a plan keeps the defaults); an
    overdue payment does in both modes."""
    now = now or datetime.now(UTC)
    subs = (
        await db.execute(
            text(
                "SELECT provider_subscription_id, plan_key, status, current_period_end, cancel_at_period_end,"
                " ended_at, past_due_since, updated_at FROM subscriptions WHERE workspace_id = :w"
                " ORDER BY (status IN ('trialing', 'active', 'past_due')) DESC, updated_at DESC"
            ),
            {"w": workspace_id},
        )
    ).all()
    customer = (
        await db.execute(
            text("SELECT disputed_at FROM billing_customers WHERE workspace_id = :w"), {"w": workspace_id}
        )
    ).one_or_none()
    trial = (
        await db.execute(
            text(
                "SELECT expires_at, revoked_at FROM entitlement_grants WHERE workspace_id = :w AND kind = 'trial'"
            ),
            {"w": workspace_id},
        )
    ).one_or_none()
    current: Any = subs[0] if subs else None
    trial_active = trial is not None and trial.revoked_at is None and trial.expires_at > now
    plan: Plan | None = None
    status: str | None = None
    if current is not None and current.status in HOLDS_PLAN:
        plan, status = PLANS[current.plan_key], current.status
    elif trial_active:
        plan, status = PLANS["trial"], "trial"
    elif current is not None:
        status = current.status
    blocked = None
    if current is not None and current.status in ("past_due", "unpaid"):
        blocked = "past_due"
    elif customer is not None and customer.disputed_at is not None:
        blocked = "disputed"
    suspended = False
    if (
        enforced
        and current is not None
        and current.status == "canceled"
        and plan is None
        and not trial_active
    ):
        ended = current.ended_at or current.updated_at  # an abandoned checkout never counts as ended
        suspended = ended < now - timedelta(days=ENDED_READ_DAYS)
    if (
        current is not None
        and current.status in ("past_due", "unpaid")
        and current.past_due_since is not None
    ):
        suspended = suspended or current.past_due_since < now - timedelta(days=PAST_DUE_GRACE_DAYS)
    return BillingState(
        plan=plan,
        status=status,
        subscription_id=current.provider_subscription_id if current is not None else None,
        period_end=current.current_period_end if current is not None else None,
        cancel_at_period_end=bool(current.cancel_at_period_end) if current is not None else False,
        trial_ends_at=trial.expires_at if trial is not None else None,
        trial_used=trial is not None,
        generation_blocked=blocked,
        hosting_suspended=suspended,
        ever_subscribed=bool(subs),
    )


async def require_generation(db: AsyncSession, workspace_id: UUID) -> None:
    """New generation (model) builds: refused while a payment is overdue or disputed (11)."""
    billing = await state(db, workspace_id)
    if billing.generation_blocked == "past_due":
        raise ApiError(
            402,
            "BILLING_PAST_DUE",
            "A payment is overdue. Update the payment method in billing to start new builds; reading continues.",
        )
    if billing.generation_blocked == "disputed":
        raise ApiError(
            402,
            "BILLING_DISPUTED",
            "A payment is disputed. Contact support to resolve it; reading continues.",
        )


async def active_build_limit(db: AsyncSession, settings: Settings, workspace_id: UUID) -> int:
    billing = await state(db, workspace_id)
    return billing.plan.active_builds if billing.plan else settings.max_active_builds_per_workspace


async def require_capacity(db: AsyncSession, settings: Settings, workspace_id: UUID, what: str) -> None:
    """Plan limits on new writes (11): `projects` or `seats`. Existing data is never removed."""
    if not settings.billing_enforced:
        return
    # Serialize concurrent creates of the workspace so two requests cannot both take the last slot.
    await db.execute(text("SELECT 1 FROM workspaces WHERE id = :w FOR UPDATE"), {"w": workspace_id})
    billing = await state(db, workspace_id)
    if billing.plan is None:
        raise ApiError(402, "PLAN_REQUIRED", "Choose a plan or start the trial to continue.")
    if what == "projects":
        used = (
            await db.execute(
                text("SELECT count(*) FROM projects WHERE workspace_id = :w AND deleted_at IS NULL"),
                {"w": workspace_id},
            )
        ).scalar_one()
        limit = billing.plan.projects
    else:
        used = (
            await db.execute(
                text(
                    "SELECT (SELECT count(*) FROM memberships WHERE workspace_id = :w)"
                    " + (SELECT count(*) FROM invitations WHERE workspace_id = :w AND accepted_at IS NULL"
                    "    AND revoked_at IS NULL AND expires_at > now())"
                ),
                {"w": workspace_id},
            )
        ).scalar_one()
        limit = billing.plan.seats
    if used >= limit:
        raise ApiError(
            409,
            "PLAN_LIMIT",
            f"The {billing.plan.name} plan allows {limit} {what}. Upgrade the plan to add more.",
            details={"limit": limit, "kind": what},
        )
