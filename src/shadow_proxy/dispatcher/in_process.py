"""In-process bounded asyncio queue with a worker pool.

Design notes:

- ``enqueue`` never blocks — uses ``put_nowait``. On overflow it drops (either
  the new job or the oldest, per policy) and returns ``DispatchStatus.DROPPED``.
  This keeps the primary path fast and predictable.
- Workers are ``asyncio.Task`` s that ``await queue.get()`` and delegate to the
  supplied handler. Handler errors are logged but must not crash the worker.
- ``stop(drain=True)`` waits for in-flight jobs to complete before returning.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from shadow_proxy.dispatcher.base import (
    CandidateDispatcher,
    CandidateJob,
    DispatchStatus,
    JobHandler,
)
from shadow_proxy.observability import Metrics, get_logger

_log = get_logger("shadow_proxy.dispatcher")

OverflowPolicy = Literal["drop_new", "drop_old"]


class InProcessDispatcher(CandidateDispatcher):
    def __init__(
        self,
        *,
        capacity: int,
        workers: int,
        overflow_policy: OverflowPolicy = "drop_new",
        metrics: Metrics | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        if workers < 1:
            raise ValueError("workers must be >= 1")
        self._queue: asyncio.Queue[CandidateJob] = asyncio.Queue(maxsize=capacity)
        self._workers_n = workers
        self._policy: OverflowPolicy = overflow_policy
        self._metrics = metrics
        self._tasks: list[asyncio.Task[None]] = []
        self._handler: JobHandler | None = None
        self._running = False

    def depth(self) -> int:
        return self._queue.qsize()

    async def start(self, handler: JobHandler) -> None:
        if self._running:
            return
        self._handler = handler
        self._running = True
        for i in range(self._workers_n):
            self._tasks.append(asyncio.create_task(self._worker_loop(i), name=f"cand-worker-{i}"))
        _log.info(
            "dispatcher.started",
            workers=self._workers_n,
            capacity=self._queue.maxsize,
            policy=self._policy,
        )

    async def enqueue(self, job: CandidateJob) -> DispatchStatus:
        try:
            self._queue.put_nowait(job)
            if self._metrics is not None:
                self._metrics.candidate_queue_depth.set(self._queue.qsize())
            return DispatchStatus.ENQUEUED
        except asyncio.QueueFull:
            if self._policy == "drop_old":
                try:
                    dropped = self._queue.get_nowait()
                    self._queue.task_done()
                    _log.warning("dispatcher.dropped_old", request_id=dropped.request_id)
                    if self._metrics is not None:
                        self._metrics.candidate_dropped_total.labels(policy="drop_old").inc()
                    self._queue.put_nowait(job)
                    return DispatchStatus.ENQUEUED
                except asyncio.QueueEmpty:
                    pass
            _log.warning("dispatcher.dropped_new", request_id=job.request_id)
            if self._metrics is not None:
                self._metrics.candidate_dropped_total.labels(policy=self._policy).inc()
            return DispatchStatus.DROPPED

    async def stop(self, *, drain: bool = True) -> None:
        if not self._running:
            return
        self._running = False
        if drain:
            await self._queue.join()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        _log.info("dispatcher.stopped")

    async def _worker_loop(self, worker_id: int) -> None:
        assert self._handler is not None
        while True:
            job = await self._queue.get()
            try:
                await self._handler(job)
            except asyncio.CancelledError:
                self._queue.task_done()
                raise
            except Exception as exc:  # noqa: BLE001
                _log.exception(
                    "dispatcher.handler_error",
                    worker=worker_id,
                    request_id=job.request_id,
                    error=str(exc),
                )
            finally:
                self._queue.task_done()
                if self._metrics is not None:
                    self._metrics.candidate_queue_depth.set(self._queue.qsize())
