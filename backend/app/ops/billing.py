"""`python -m app.ops.billing reconcile|process`: billing recovery commands (22 "Billing incidents").

- reconcile: re-sync every customer's subscriptions from the provider and apply the hosting rules. Run it
  after a webhook outage, after restoring the database (the provider is the record of payments), or when
  a workspace reports that a payment is not reflected.
- process: process received Stripe webhook receipts now instead of waiting for the worker.

Runs with the runtime role (DATABASE_URL), like the worker. Prints a JSON summary; exits 1 when any
customer or receipt failed.
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx


async def _run(command: str) -> int:
    from app.adapters.billing import billing_provider
    from app.core.db import create_engine, session_factory
    from app.core.logging import configure_logging
    from app.core.settings import get_settings
    from app.main import HTTP_TIMEOUT
    from app.modules.billing import sync

    configure_logging()
    settings = get_settings()
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=False) as http:
        provider = billing_provider(settings, http)
        if provider is None:
            print("billing is not configured (STRIPE_SECRET_KEY)", file=sys.stderr)
            return 2
        engine = create_engine(settings, application_name="billing-ops")
        try:
            sessions = session_factory(engine)
            if command == "reconcile":
                result = await sync.reconcile(sessions, provider, settings)
                result["hosting_changed"] = await sync.sweep_hosting(
                    sessions, enforced=settings.billing_enforced
                )
                failed = result["failed"]
            else:
                result = await sync.process_pending(sessions, provider, settings)
                failed = result.get("failed", 0) + result.get("retry", 0)
        finally:
            await engine.dispose()
    print(json.dumps(result, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("reconcile", "process"):
        print("usage: python -m app.ops.billing reconcile|process", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(_run(sys.argv[1])))
