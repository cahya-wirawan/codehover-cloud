"""A stage stops as soon as its lease is lost (04 fencing; A22 deletion fences running work)."""

from __future__ import annotations

import asyncio

import pytest

from app.jobs import store
from app.jobs.runner import _until_lost


async def test_a_lost_lease_cancels_the_running_stage() -> None:
    lost = asyncio.Event()
    stopped = asyncio.Event()
    stored: list[str] = []

    async def stage() -> str:
        try:
            for n in range(100):
                stored.append(f"object-{n}")
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            stopped.set()
            raise
        return "done"

    asyncio.get_running_loop().call_later(0.03, lost.set)
    with pytest.raises(store.LeaseLost):
        await _until_lost(stage(), lost)
    assert stopped.is_set() and len(stored) < 100  # it stopped writing


async def test_a_finished_stage_returns_its_result_and_errors_propagate() -> None:
    async def ok() -> int:
        return 7

    assert await _until_lost(ok(), asyncio.Event()) == 7

    async def broken() -> None:
        raise ValueError("stage error")

    with pytest.raises(ValueError):
        await _until_lost(broken(), asyncio.Event())
