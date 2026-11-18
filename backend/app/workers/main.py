"""`python -m app.workers.main`: task consumer (04). Refuses to start without network isolation on Linux.

Beside the consumer, a maintenance loop purges due deletions (app.jobs.purge) and, hourly, sweeps
retention (app.jobs.retention), with the same runtime role, object store and tenant context rules as
every stage."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import logging
import sys
import time

import aio_pika
from pydantic import ValidationError

from app.core import metrics
from app.core.settings import get_settings
from app.engine import sandbox
from app.jobs import retention
from app.jobs.envelope import TaskEnvelope
from app.jobs.purge import purge_due
from app.jobs.queue import topology
from app.jobs.runner import process
from app.jobs.runtime import build_runtime
from app.jobs.stages import Runtime
from app.workers._shell import run

logger = logging.getLogger("app.workers.main")
MAINTENANCE_SECONDS = 30.0
RETENTION_SECONDS = 3600.0


def _not_dumpable() -> None:
    """PR_SET_DUMPABLE=0: same-UID processes (the sandboxed parser) can neither ptrace this worker nor read
    its /proc/<pid>/environ, which holds database, broker and storage credentials."""
    if sys.platform == "linux":
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
            raise OSError(ctypes.get_errno(), "PR_SET_DUMPABLE failed")


async def maintenance(runtime: Runtime, stop: asyncio.Event) -> None:
    last_sweep = 0.0
    while not stop.is_set():
        try:
            await purge_due(runtime.sessions, runtime.store)
            if time.monotonic() - last_sweep >= RETENTION_SECONDS:
                counts = await retention.sweep(runtime.sessions, runtime.store)
                last_sweep = time.monotonic()
                logger.info("retention swept", extra=counts)
        except Exception:
            logger.exception("maintenance cycle failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=MAINTENANCE_SECONDS)


async def body(stop: asyncio.Event) -> None:
    _not_dumpable()
    settings = get_settings()
    metrics.serve(settings.metrics_port)
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
        housekeeping = asyncio.create_task(maintenance(runtime, stop))
        logger.info("process ready", extra={"role": "worker"})
        await stop.wait()
        await housekeeping
        with contextlib.suppress(Exception):
            await queue.cancel(tag)
    await http.aclose()


if __name__ == "__main__":
    run("worker", body)
