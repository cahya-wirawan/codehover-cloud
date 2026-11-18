"""`python -m app.ops.billing reconcile|process|clear-dispute`: billing recovery commands (22).

- reconcile: re-sync every customer's subscriptions from the provider and apply the hosting rules. Run it
  after a webhook outage, after restoring the database (the provider is the record of payments), or when
  a workspace reports that a payment is not reflected.
- process: process received Stripe webhook receipts now instead of waiting for the worker.
- clear-dispute <workspace-id> "<reason>": after a lost dispute is settled with the customer, let the
  workspace start generation builds again (open disputes still block); audited in the workspace.

Runs with the runtime role (DATABASE_URL), like the worker. Prints a JSON summary; exits 1 when any
customer or receipt failed.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any
from uuid import UUID

import httpx


async def _clear(workspace_id: UUID, reason: str) -> int:
    from app.core.db import create_engine, session_factory
    from app.core.settings import get_settings
    from app.modules.billing import sync

    engine = create_engine(get_settings(), application_name="billing-ops")
    try:
        cleared = await sync.clear_lost_dispute(session_factory(engine), workspace_id, reason)
    finally:
        await engine.dispose()
    print(json.dumps({"cleared": cleared}))
    return 0 if cleared else 1


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
            result: dict[str, Any]
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


USAGE = 'usage: python -m app.ops.billing reconcile|process|clear-dispute <workspace-id> "<reason>"'

if __name__ == "__main__":
    if sys.argv[1:2] == ["clear-dispute"] and len(sys.argv) == 4 and len(sys.argv[3].strip()) >= 10:
        sys.exit(asyncio.run(_clear(UUID(sys.argv[2]), sys.argv[3].strip())))
    if len(sys.argv) != 2 or sys.argv[1] not in ("reconcile", "process"):
        print(USAGE + "  (a reason of at least 10 characters)", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(_run(sys.argv[1])))
