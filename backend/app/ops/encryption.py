"""`python -m app.ops.encryption reseal|check`: rotate APP_ENCRYPTION_KEY without losing stored secrets (22).

Workspaces' own provider keys are sealed with APP_ENCRYPTION_KEY (AES-256-GCM, bound to the workspace and
provider). To rotate it:

1. Set APP_ENCRYPTION_KEY to the new key and APP_ENCRYPTION_KEY_PREVIOUS to the old one, then restart the
   API and the workers. New values are sealed with the new key; old ones still open with the previous key.
2. Run `reseal`: every live provider key the previous key sealed is sealed again with the new key.
3. Run `check` (it changes nothing): it exits 0 once no stored key needs the previous key.
4. Wait at least ten minutes (sign-in and GitHub install states last 600 s and are never resealed), unset
   APP_ENCRYPTION_KEY_PREVIOUS and restart again.

Runs with the runtime role (DATABASE_URL), in each workspace's own tenant context like the worker, so it
needs no privilege that bypasses row-level security. Idempotent. Prints a JSON summary and exits 1 when a
key opens with neither key (it stays as it is; its workspace must save it again) or, for `check`, when a
key still needs the previous one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from uuid import UUID

from cryptography.exceptions import InvalidTag
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import Sealer
from app.core.tenancy import set_context
from app.modules.providers.keys import associated

logger = logging.getLogger("app.ops.encryption")
PAGE = 100
LIVE = "SELECT id, provider, sealed_key FROM provider_keys WHERE workspace_id = :w AND revoked_at IS NULL"
LOCKED = LIVE + " FOR UPDATE"  # a key replaced meanwhile waits, so the new value is never overwritten


async def reseal_workspace(
    sessions: async_sessionmaker[AsyncSession], sealer: Sealer, workspace_id: UUID, *, write: bool
) -> dict[str, int]:
    counts = {"keys": 0, "resealed": 0, "unreadable": 0}
    async with sessions() as db:
        await set_context(db, workspace_id=workspace_id)
        rows = (await db.execute(text(LOCKED if write else LIVE), {"w": workspace_id})).all()
        for row in rows:
            counts["keys"] += 1
            try:
                fresh = sealer.reseal(bytes(row.sealed_key), associated(workspace_id, row.provider))
            except InvalidTag:
                counts["unreadable"] += 1
                logger.error("provider key opens with neither key", extra={"workspace_id": str(workspace_id)})
                continue
            if fresh is None:
                continue
            counts["resealed"] += 1
            if write:
                await db.execute(
                    text("UPDATE provider_keys SET sealed_key = :k WHERE workspace_id = :w AND id = :id"),
                    {"k": fresh, "w": workspace_id, "id": row.id},
                )
        await db.commit()
    return counts


async def reseal(
    sessions: async_sessionmaker[AsyncSession], sealer: Sealer, *, write: bool = True
) -> dict[str, int]:
    """Seal every live provider key with the current key (or, without `write`, count those that need it)."""
    totals = {"workspaces": 0, "keys": 0, "resealed": 0, "unreadable": 0}
    after: UUID | None = None
    while True:
        async with sessions() as db:
            page = list(
                (
                    await db.execute(
                        text("SELECT app_private.workspaces_after(:a, :n)"), {"a": after, "n": PAGE}
                    )
                ).scalars()
            )
            await db.commit()
        if not page:
            return totals
        for workspace_id in page:
            totals["workspaces"] += 1
            for name, value in (await reseal_workspace(sessions, sealer, workspace_id, write=write)).items():
                totals[name] += value
        after = page[-1]


async def _run(command: str) -> int:
    from app.core.db import create_engine, session_factory
    from app.core.logging import configure_logging
    from app.core.security import sealer_for
    from app.core.settings import get_settings

    configure_logging()
    settings = get_settings()
    if command == "reseal" and settings.app_encryption_key_previous is None:
        print(
            "set APP_ENCRYPTION_KEY_PREVIOUS to the old key first (see python -m app.ops.encryption --help)",
            file=sys.stderr,
        )
        return 2
    engine = create_engine(settings, application_name="encryption-ops")
    try:
        result = await reseal(session_factory(engine), sealer_for(settings), write=command == "reseal")
    finally:
        await engine.dispose()
    if command == "check":
        result["need_previous"] = result.pop("resealed")
    print(json.dumps(result, sort_keys=True))
    return 1 if result["unreadable"] or result.get("need_previous") else 0


USAGE = "usage: python -m app.ops.encryption reseal|check"

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("reseal", "check"):
        print(USAGE, file=sys.stderr)
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(_run(sys.argv[1])))
