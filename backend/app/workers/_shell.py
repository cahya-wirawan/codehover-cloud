"""Shared process shell for background roles.

M0: connect to PostgreSQL with the role's credentials, report readiness, then idle until SIGTERM.
M1 replaces the idle loop with the real consumer/dispatcher/scheduler (backlog T07).
"""

from __future__ import annotations

import asyncio
import logging
import signal

from sqlalchemy import text

from app import __version__
from app.core.db import create_engine
from app.core.logging import configure_logging
from app.core.settings import get_settings

logger = logging.getLogger("app.workers")

CONNECT_ATTEMPTS = 10
MAX_BACKOFF_SECONDS = 10.0


async def _wait_for_database(role: str) -> None:
    settings = get_settings()
    engine = create_engine(settings, application_name=role)
    delay = 0.5
    try:
        for attempt in range(1, CONNECT_ATTEMPTS + 1):
            try:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                return
            except Exception as exc:
                if attempt == CONNECT_ATTEMPTS:
                    raise
                logger.warning(
                    "database not reachable yet",
                    extra={"role": role, "attempt": attempt, "error_type": type(exc).__name__},
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF_SECONDS)
    finally:
        await engine.dispose()


async def _run(role: str) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await _wait_for_database(role)
    logger.info(
        "process ready (M0 shell: no task handling yet)", extra={"role": role, "version": __version__}
    )
    await stop.wait()
    logger.info("shutting down", extra={"role": role})


def run(role: str) -> None:
    configure_logging(get_settings().log_level)
    asyncio.run(_run(role))
