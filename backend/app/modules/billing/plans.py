"""Plans (11 "Monetization and billing"). The figures are the package's plan hypotheses; prices live in the
provider and are mapped by key from operator settings, so the browser never chooses an amount (11)."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.settings import Settings


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    allowance_micro_usd: int  # included provider-cost allowance per month (trial: once)
    seats: int
    projects: int
    storage_gib: int
    active_builds: int


PLANS: dict[str, Plan] = {
    "trial": Plan("trial", "Trial", 2_000_000, seats=1, projects=1, storage_gib=1, active_builds=1),
    "pro": Plan("pro", "Pro", 5_000_000, seats=1, projects=10, storage_gib=5, active_builds=1),
    "team": Plan("team", "Team", 20_000_000, seats=5, projects=50, storage_gib=25, active_builds=2),
}
PAID = ("pro", "team")
TRIAL_DAYS = 14
PAST_DUE_GRACE_DAYS = 7  # reading continues this long after a payment fails (11)
ENDED_READ_DAYS = 30  # after a subscription ends, read and export continue this long (11)

# Provider statuses that include the plan's allowance; past_due keeps the plan but blocks generation.
ENTITLED = ("trialing", "active")
HOLDS_PLAN = ("trialing", "active", "past_due")


def price_for(settings: Settings, plan_key: str) -> str | None:
    return {"pro": settings.stripe_price_pro, "team": settings.stripe_price_team}.get(plan_key)


def plan_for_price(settings: Settings, price_id: str | None) -> str | None:
    for key in PAID:
        if price_id and price_for(settings, key) == price_id:
            return key
    return None
