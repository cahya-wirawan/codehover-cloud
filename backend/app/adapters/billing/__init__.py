"""Payment provider adapters (11 "BillingProvider"). Only Stripe is implemented; tests use a replay fake."""

from __future__ import annotations

import httpx

from app.adapters.billing.stripe import StripeBilling
from app.core.settings import Settings


def billing_provider(settings: Settings, http: httpx.AsyncClient) -> StripeBilling | None:
    """The configured provider, or None while billing is not configured (checkout and portal answer 503)."""
    if settings.stripe_secret_key is None:
        return None
    return StripeBilling(settings.stripe_secret_key.get_secret_value(), http)
