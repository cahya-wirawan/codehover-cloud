"""Process one notification: claim, run the stage with a lease heartbeat, settle (04).

The queue message is only a hint: everything is re-read from SQL. Duplicates and stale notifications
claim nothing. The caller acknowledges the message after this returns; nothing is ever requeued by the
broker, retries are scheduled in SQL and re-notified by the reconciler.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable

from app.core import metrics
from app.jobs import store
from app.jobs.batching import StageWait
from app.jobs.envelope import TaskEnvelope
from app.jobs.stages import HANDLERS, Runtime, StageError

logger = logging.getLogger("app.jobs")


async def _heartbeat(rt: Runtime, claimed: store.Claim, lost: asyncio.Event) -> None:
    while True:
        await asyncio.sleep(store.HEARTBEAT_SECONDS)
        async with rt.sessions() as db:
            if not await store.heartbeat(db, claimed):
                lost.set()
                return


async def _until_lost[T](work: Awaitable[T], lost: asyncio.Event) -> T:
    """Run the stage until it finishes or its lease is lost. A lost lease (expired, cancelled, or fenced
    by a deletion) stops the stage at once, so it neither stores more objects nor calls providers (A22).
    Cancelling this (a worker shutting down) cancels the stage as well; the task is claimed again later."""
    task = asyncio.ensure_future(work)
    watch = asyncio.ensure_future(lost.wait())
    try:
        await asyncio.wait({task, watch}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:  # the worker is shutting down: stop the stage too, not just the wait
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        raise
    finally:
        watch.cancel()
    if not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        raise store.LeaseLost("heartbeat")
    return task.result()


async def process(rt: Runtime, envelope: TaskEnvelope) -> str:
    """Returns an outcome label for logs and tests."""
    started = time.perf_counter()
    seen: list[str] = []
    outcome = await _process(rt, envelope, seen)
    if seen:  # a claimed task: count it under its stage (bounded labels only)
        stage = metrics.label(seen[0])
        metrics.TASKS.labels(stage, outcome).inc()
        metrics.TASK_DURATION.labels(stage).observe(time.perf_counter() - started)
    return outcome


async def _process(rt: Runtime, envelope: TaskEnvelope, seen: list[str]) -> str:
    async with rt.sessions() as db:
        claimed = await store.claim(db, envelope)
    if claimed is None:
        return "not_runnable"
    seen.append(claimed.stage)
    log = {"task_id": str(claimed.task_id), "stage": claimed.stage, "attempt": claimed.attempt}
    if claimed.attempt > store.MAX_ATTEMPTS:  # a task that keeps killing workers
        async with rt.sessions() as db:
            await store.fail(db, claimed, "attempts_exhausted", transient=False)
        return "failed"
    lost = asyncio.Event()
    beat = asyncio.create_task(_heartbeat(rt, claimed, lost))
    try:
        try:
            apply = await _until_lost(HANDLERS[claimed.stage](rt, claimed), lost)
        except StageWait as waiting:
            async with rt.sessions() as db:
                outcome = await store.wait(db, claimed, waiting.seconds)
            logger.info("stage waiting", extra={**log, "seconds": waiting.seconds})
            return outcome
        except StageError as error:
            async with rt.sessions() as db:
                outcome = await store.fail(
                    db, claimed, error.code, transient=error.transient, apply=error.apply
                )
            logger.warning("stage failed", extra={**log, "error": error.code, "outcome": outcome})
            return outcome
        except Exception as error:  # unexpected: retry within the budget, never crash the consumer
            logger.exception("stage crashed", extra=log)
            async with rt.sessions() as db:
                return await store.fail(db, claimed, f"internal_{type(error).__name__}", transient=True)
        try:
            async with rt.sessions() as db:
                await store.complete(db, claimed, apply)
        except store.LeaseLost:
            raise
        except Exception as error:  # the stage's writes failed: retry within the budget, never hang leased
            logger.exception("stage apply failed", extra=log)
            async with rt.sessions() as db:
                return await store.fail(db, claimed, f"internal_{type(error).__name__}", transient=True)
        logger.info("stage completed", extra=log)
        return "completed"
    except store.LeaseLost:
        logger.warning("lease lost; result discarded", extra=log)
        return "lease_lost"
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
