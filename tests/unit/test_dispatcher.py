from __future__ import annotations

import asyncio

import pytest

from shadow_proxy.dispatcher import CandidateJob, DispatchStatus, InProcessDispatcher


@pytest.mark.asyncio()
async def test_enqueue_and_process() -> None:
    processed: list[str] = []
    ready = asyncio.Event()

    async def handler(job: CandidateJob) -> None:
        processed.append(job.request_id)
        ready.set()

    disp = InProcessDispatcher(capacity=10, workers=2)
    await disp.start(handler)
    try:
        assert await disp.enqueue(CandidateJob("a")) is DispatchStatus.ENQUEUED
        await asyncio.wait_for(ready.wait(), timeout=2.0)
    finally:
        await disp.stop(drain=True)

    assert processed == ["a"]


@pytest.mark.asyncio()
async def test_overflow_drop_new() -> None:
    hold = asyncio.Event()

    async def handler(_: CandidateJob) -> None:
        await hold.wait()

    disp = InProcessDispatcher(capacity=1, workers=1, overflow_policy="drop_new")
    await disp.start(handler)
    try:
        # Fill worker + queue
        assert (await disp.enqueue(CandidateJob("a"))) is DispatchStatus.ENQUEUED
        # Give the worker time to pick up "a" so the queue is empty again.
        await asyncio.sleep(0)
        assert (await disp.enqueue(CandidateJob("b"))) is DispatchStatus.ENQUEUED
        # Now capacity is full.
        assert (await disp.enqueue(CandidateJob("c"))) is DispatchStatus.DROPPED
    finally:
        hold.set()
        await disp.stop(drain=True)


@pytest.mark.asyncio()
async def test_handler_error_does_not_kill_worker() -> None:
    calls = 0
    done = asyncio.Event()

    async def handler(job: CandidateJob) -> None:
        nonlocal calls
        calls += 1
        if job.request_id == "boom":
            raise RuntimeError("nope")
        done.set()

    disp = InProcessDispatcher(capacity=10, workers=1)
    await disp.start(handler)
    try:
        await disp.enqueue(CandidateJob("boom"))
        await disp.enqueue(CandidateJob("ok"))
        await asyncio.wait_for(done.wait(), timeout=2.0)
    finally:
        await disp.stop(drain=True)
    assert calls == 2
