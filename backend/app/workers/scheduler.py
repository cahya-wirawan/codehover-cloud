"""`python -m app.workers.scheduler`: lease expiry and retry/lost-message re-notification (app_system)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from app.core.db import create_engine, session_factory
from app.core.settings import get_settings
from app.jobs.control import reconcile_once, sweep_outbox
from app.workers._shell import run

logger = logging.getLogger("app.workers.scheduler")
INTERVAL_SECONDS = 5.0
OUTBOX_SWEEP_SECONDS = 3600.0


async def body(stop: asyncio.Event) -> None:
    sessions = session_factory(create_engine(get_settings(), application_name="scheduler"))
    logger.info("process ready", extra={"role": "scheduler"})
    last_sweep = 0.0
    while not stop.is_set():
        try:
            async with sessions() as db:
                counts = await reconcile_once(db)
            if any(counts.values()):
                logger.info("reconciled", extra=counts)
            if time.monotonic() - last_sweep >= OUTBOX_SWEEP_SECONDS:
                async with sessions() as db:
                    removed = await sweep_outbox(db)
                last_sweep = time.monotonic()
                if removed:
                    logger.info("outbox swept", extra={"removed": removed})
        except Exception:
            logger.exception("reconcile cycle failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=INTERVAL_SECONDS)


if __name__ == "__main__":
    run("scheduler", body)
