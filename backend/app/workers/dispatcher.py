"""`python -m app.workers.dispatcher`: outbox -> RabbitMQ with publisher confirms (runs as app_system)."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import aio_pika

from app.core.db import create_engine, session_factory
from app.core.settings import get_settings
from app.jobs.control import dispatch_once
from app.jobs.queue import topology
from app.workers._shell import run

logger = logging.getLogger("app.workers.dispatcher")
IDLE_SECONDS = 1.0


async def body(stop: asyncio.Event) -> None:
    settings = get_settings()
    sessions = session_factory(create_engine(settings, application_name="dispatcher"))
    assert settings.amqp_url is not None, "AMQP_URL is required"
    connection = await aio_pika.connect_robust(settings.amqp_url.get_secret_value())
    async with connection:
        channel = await connection.channel(publisher_confirms=True)
        exchange, _ = await topology(channel)
        logger.info("process ready", extra={"role": "dispatcher"})
        while not stop.is_set():
            try:
                async with sessions() as db:
                    published = await dispatch_once(db, exchange)
            except Exception:
                logger.exception("dispatch cycle failed")
                published = 0
            if not published:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=IDLE_SECONDS)


if __name__ == "__main__":
    run("dispatcher", body)
