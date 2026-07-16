"""Periodic reconciler.

Finds rows stuck in ``candidate_status IN ('pending', 'in_progress')`` older
than ``stale_threshold_s`` and marks them ``timeout_stale``. This guarantees
that no row is left dangling if a worker dies mid-call.

Also has a ``requeue_all_unfinished`` helper used at startup to re-inject
work into the dispatcher after a restart.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from shadow_proxy.dispatcher import CandidateDispatcher, CandidateJob
from shadow_proxy.evaluator import Verdict
from shadow_proxy.observability import Metrics, get_logger
from shadow_proxy.store import (
    CandidateStatus,
    ComparisonStore,
    FinalizePatch,
)

_log = get_logger("shadow_proxy.sweeper")


class Sweeper:
    def __init__(
        self,
        *,
        store: ComparisonStore,
        interval_s: float,
        stale_threshold_s: float,
        metrics: Metrics | None = None,
    ) -> None:
        self._store = store
        self._interval = interval_s
        self._threshold = timedelta(seconds=stale_threshold_s)
        self._metrics = metrics
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="sweeper")
        _log.info(
            "sweeper.started",
            interval_s=self._interval,
            stale_threshold_s=self._threshold.total_seconds(),
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=self._interval + 5)
        except TimeoutError:
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001
                _log.exception("sweeper.tick_error", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass

    async def run_once(self) -> int:
        stale_ids = await self._store.find_stale(self._threshold, limit=1000)
        if not stale_ids:
            return 0
        _log.info("sweeper.stale_found", count=len(stale_ids))
        now = datetime.now(UTC)
        for request_id in stale_ids:
            await self._store.finalize(
                request_id,
                FinalizePatch(
                    candidate_status=CandidateStatus.TIMEOUT_STALE,
                    verdict=Verdict.CANDIDATE_ERROR,
                    reasons=["timeout_stale"],
                    evaluated_at=now,
                ),
            )
        if self._metrics is not None:
            self._metrics.sweeper_reconciled_total.inc(len(stale_ids))
        return len(stale_ids)


async def requeue_all_unfinished(
    store: ComparisonStore, dispatcher: CandidateDispatcher, *, limit: int = 10_000
) -> int:
    """Called at startup: push every unfinished row back into the dispatcher."""
    ids = await store.find_all_unfinished(limit=limit)
    for rid in ids:
        await dispatcher.enqueue(CandidateJob(request_id=rid))
    if ids:
        _log.info("startup.requeued_unfinished", count=len(ids))
    return len(ids)
