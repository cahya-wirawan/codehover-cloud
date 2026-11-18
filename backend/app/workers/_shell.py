"""Shared process lifecycle for background roles: JSON logs, SIGTERM-driven graceful stop."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable

from app.core.logging import configure_logging
from app.core.settings import get_settings

logger = logging.getLogger("app.workers")


def run(role: str, body: Callable[[asyncio.Event], Awaitable[None]]) -> None:
    configure_logging(get_settings().log_level)

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        logger.info("process starting", extra={"role": role})
        await body(stop)
        logger.info("shutting down", extra={"role": role})

    asyncio.run(main())
