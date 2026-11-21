"""bounded(): the per-build concurrency gate for provider calls (07 "at most 4 concurrent calls").

After the first unit stops the stage, no further unit starts, but units already running finish, so
their provider calls settle at actual cost instead of being cut off with an unknown outcome (06 cancel:
"in-flight calls settle"). Cancelling bounded() itself, as a worker shutdown does, still cancels them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from app.jobs import generation


class Stop(Exception):
    pass


def unit(log: list[str], name: str, delay: float, *, fail: bool = False) -> Callable[[], Awaitable[str]]:
    async def run() -> str:
        log.append(f"start {name}")
        await asyncio.sleep(delay)
        if fail:
            raise Stop(name)
        log.append(f"done {name}")
        return name

    return run


async def test_results_keep_their_order_and_at_most_four_run_at_once() -> None:
    log: list[str] = []
    items = [unit(log, str(i), 0.01 * (5 - i)) for i in range(6)]
    assert await generation.bounded(items) == [str(i) for i in range(6)]
    running = peak = 0
    for entry in log:
        running += 1 if entry.startswith("start") else -1
        peak = max(peak, running)
    assert peak == generation.MAX_CONCURRENT_CALLS


async def test_a_stop_lets_running_units_finish_and_starts_no_more() -> None:
    log: list[str] = []
    slow = 0.2
    items = [unit(log, "a", 0.01, fail=True)]  # stops the stage while b, c and d are still running
    items += [unit(log, name, slow) for name in ("b", "c", "d")]
    items += [unit(log, name, slow) for name in ("e", "f")]  # queued behind the gate of 4
    with pytest.raises(Stop, match="a"):
        await generation.bounded(items)
    assert {"done b", "done c", "done d"} <= set(log)  # in-flight calls ran to completion
    assert not any(e.endswith((" e", " f")) for e in log)  # nothing started after the stop


async def test_the_first_stop_wins() -> None:
    log: list[str] = []
    items = [unit(log, "late", 0.1, fail=True), unit(log, "early", 0.01, fail=True)]
    with pytest.raises(Stop, match="early"):
        await generation.bounded(items)
    assert "start late" in log  # the later failure still ran; its error is not the one raised


async def test_cancelling_bounded_cancels_every_unit() -> None:
    log: list[str] = []
    task = asyncio.create_task(generation.bounded([unit(log, name, 5.0) for name in ("x", "y")]))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert log == ["start x", "start y"]  # neither finished: a shutdown does not wait for calls
