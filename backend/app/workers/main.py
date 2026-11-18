"""`python -m app.workers.main`: task consumer (04). Refuses to start without network isolation on Linux."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import aio_pika
from pydantic import ValidationError

from app.core.settings import get_settings
from app.engine import sandbox
from app.jobs.envelope import TaskEnvelope
from app.jobs.queue import topology
from app.jobs.runner import process
from app.jobs.runtime import build_runtime
from app.workers._shell import run

logger = logging.getLogger("app.workers.main")


async def body(stop: asyncio.Event) -> None:
    settings = get_settings()
    runtime, http = build_runtime(settings, "worker")
    report = await sandbox.self_check(allow_unsandboxed=runtime.allow_unsandboxed)
    logger.info("sandbox verified", extra={"isolated": report.get("isolated")})
    assert settings.amqp_url is not None, "AMQP_URL is required"
    connection = await aio_pika.connect_robust(settings.amqp_url.get_secret_value())
    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=1)  # expensive tasks: one at a time per worker
        _, queue = await topology(channel)

        async def on_message(message: aio_pika.abc.AbstractIncomingMessage) -> None:
            try:
                envelope = TaskEnvelope.model_validate_json(message.body)
            except ValidationError:
                logger.warning("invalid task envelope dropped", extra={"message_id": message.message_id})
                await message.reject(requeue=False)  # poison message -> dead-letter exchange
                return
            outcome = await process(runtime, envelope)
            logger.info("task processed", extra={"task_id": str(envelope.task_id), "outcome": outcome})
            await message.ack()  # after the SQL outcome is durable; never requeue

        tag = await queue.consume(on_message)
        logger.info("process ready", extra={"role": "worker"})
        await stop.wait()
        with contextlib.suppress(Exception):
            await queue.cancel(tag)
    await http.aclose()


if __name__ == "__main__":
    run("worker", body)
