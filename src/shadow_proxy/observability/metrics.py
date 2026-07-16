"""Prometheus metrics.

Exposes counters, histograms and gauges relevant to a shadow proxy:

- primary / candidate call counts + latency histograms
- verdict counters (match, mismatch, invalid_json, candidate_error)
- queue depth gauge, drops counter
- sweeper reconciliation counters
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()

        self.requests_total = Counter(
            "shadow_proxy_requests_total",
            "Total customer requests received.",
            ["route", "status"],
            registry=self.registry,
        )
        self.primary_latency = Histogram(
            "shadow_proxy_primary_latency_seconds",
            "Latency of primary LLM calls.",
            ["route", "model", "outcome"],
            registry=self.registry,
        )
        self.candidate_latency = Histogram(
            "shadow_proxy_candidate_latency_seconds",
            "Latency of candidate LLM calls (background).",
            ["route", "model", "outcome"],
            registry=self.registry,
        )
        self.verdicts_total = Counter(
            "shadow_proxy_verdicts_total",
            "Count of evaluation verdicts.",
            ["route", "verdict"],
            registry=self.registry,
        )
        self.candidate_queue_depth = Gauge(
            "shadow_proxy_candidate_queue_depth",
            "Current depth of the candidate dispatcher queue.",
            registry=self.registry,
        )
        self.candidate_dropped_total = Counter(
            "shadow_proxy_candidate_dropped_total",
            "Number of candidate jobs dropped due to backpressure.",
            ["policy"],
            registry=self.registry,
        )
        self.sweeper_reconciled_total = Counter(
            "shadow_proxy_sweeper_reconciled_total",
            "Rows reconciled by the sweeper (stale pending -> timeout_stale).",
            registry=self.registry,
        )
        self.store_failures_total = Counter(
            "shadow_proxy_store_failures_total",
            "Failures interacting with the ComparisonStore.",
            ["op"],
            registry=self.registry,
        )


def metrics_router() -> APIRouter:
    """Router that reads the registry off ``app.state.metrics`` at request time."""
    router = APIRouter()

    @router.get("/metrics", include_in_schema=False)
    async def _metrics(request: Request) -> Response:  # type: ignore[no-untyped-def]
        metrics: Metrics = request.app.state.metrics
        payload = generate_latest(metrics.registry)
        return Response(content=payload, media_type=CONTENT_TYPE_LATEST)

    return router
