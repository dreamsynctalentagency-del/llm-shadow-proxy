"""Reporting endpoints for shadow comparison results."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from shadow_proxy.api.deps import get_config, get_raw_store, get_settings, get_store
from shadow_proxy.evaluator import Verdict
from shadow_proxy.settings import AppConfig, Settings
from shadow_proxy.store import ComparisonStore, EvaluationFilters, RawStore

router = APIRouter(prefix="/v1", tags=["evaluations"])


class RecordOut(BaseModel):
    request_id: str
    received_at: datetime
    route: str
    tenant_id: str | None
    session_id: str | None
    request_hash: str
    raw_object_key: str | None
    primary_model: str
    primary_status: str
    primary_latency_ms: int
    primary_action: str | None
    primary_error: str | None
    candidate_model: str
    candidate_status: str
    candidate_latency_ms: int | None
    candidate_action: str | None
    candidate_error: str | None
    verdict: str
    reasons: list[Any]
    attempt_count: int
    last_attempted_at: datetime | None
    evaluated_at: datetime | None


class SummaryOut(BaseModel):
    window_seconds: int
    total: int
    match_rate: float
    verdicts: dict[str, int]
    latency_p50_ms: dict[str, float | None]
    latency_p95_ms: dict[str, float | None]


class ConfigOut(BaseModel):
    routes: dict[str, dict[str, Any]]
    evaluator: dict[str, Any]
    dispatcher: dict[str, Any]
    store: dict[str, Any]
    env_file: str | None = None
    do_inference_base_url: str | None = None
    do_inference_key: str | None = None


class RawOut(BaseModel):
    """Full LLM payloads pulled from the RawStore for a single request."""

    request_id: str
    raw_object_key: str | None = None
    primary_model: str | None = None
    candidate_model: str | None = None
    primary_content: str | None = None
    candidate_content: str | None = None
    primary_response: dict[str, Any] | None = None
    candidate_response: dict[str, Any] | None = None
    primary_error: str | None = None
    candidate_error: str | None = None
    detail: str | None = None


def _extract_message_content(resp: dict[str, Any] | None) -> str | None:
    if not resp:
        return None
    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return content if isinstance(content, str) else None


def _to_out(record: Any) -> RecordOut:
    return RecordOut(
        request_id=record.request_id,
        received_at=record.received_at,
        route=record.route,
        tenant_id=record.tenant_id,
        session_id=record.session_id,
        request_hash=record.request_hash,
        raw_object_key=record.raw_object_key,
        primary_model=record.primary_model,
        primary_status=record.primary_status.value,
        primary_latency_ms=record.primary_latency_ms,
        primary_action=record.primary_action,
        primary_error=record.primary_error,
        candidate_model=record.candidate_model,
        candidate_status=record.candidate_status.value,
        candidate_latency_ms=record.candidate_latency_ms,
        candidate_action=record.candidate_action,
        candidate_error=record.candidate_error,
        verdict=record.verdict.value,
        reasons=record.reasons,
        attempt_count=record.attempt_count,
        last_attempted_at=record.last_attempted_at,
        evaluated_at=record.evaluated_at,
    )


@router.get("/evaluations", response_model=list[RecordOut])
async def list_evaluations(
    since: datetime | None = None,
    until: datetime | None = None,
    verdict: Verdict | None = None,
    route: str | None = None,
    tenant_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    store: ComparisonStore = Depends(get_store),
) -> list[RecordOut]:
    filters = EvaluationFilters(
        since=since,
        until=until,
        verdict=verdict,
        route=route,
        tenant_id=tenant_id,
        limit=limit,
        offset=offset,
    )
    records = await store.list(filters)
    return [_to_out(r) for r in records]


@router.get("/evaluations/summary", response_model=SummaryOut)
async def evaluations_summary(
    window_seconds: int = Query(default=86400, ge=60, le=30 * 86400),
    store: ComparisonStore = Depends(get_store),
) -> SummaryOut:
    stats = await store.summary(timedelta(seconds=window_seconds))
    return SummaryOut(
        window_seconds=stats.window_seconds,
        total=stats.total,
        match_rate=stats.match_rate,
        verdicts=stats.verdicts,
        latency_p50_ms={
            "primary": stats.primary_latency_p50_ms,
            "candidate": stats.candidate_latency_p50_ms,
        },
        latency_p95_ms={
            "primary": stats.primary_latency_p95_ms,
            "candidate": stats.candidate_latency_p95_ms,
        },
    )


@router.get("/evaluations/{request_id}", response_model=RecordOut)
async def get_evaluation(
    request_id: str,
    store: ComparisonStore = Depends(get_store),
) -> RecordOut:
    record = await store.get(request_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return _to_out(record)


@router.get("/evaluations/{request_id}/raw", response_model=RawOut)
async def get_evaluation_raw(
    request_id: str,
    store: ComparisonStore = Depends(get_store),
    raw_store: RawStore | None = Depends(get_raw_store),
) -> RawOut:
    """Return the full LLM payloads (primary + candidate) stored in RawStore.

    Falls back to a partial response with ``detail`` if the raw payload is
    unavailable (e.g. RawStore disabled, object expired, or evict-by-policy).
    """
    record = await store.get(request_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    base = RawOut(
        request_id=request_id,
        raw_object_key=record.raw_object_key,
        primary_model=record.primary_model,
        candidate_model=record.candidate_model,
        primary_error=record.primary_error,
        candidate_error=record.candidate_error,
    )
    if raw_store is None:
        base.detail = "raw store not configured"
        return base
    if not record.raw_object_key:
        base.detail = "no raw payload archived for this request"
        return base

    try:
        payload = await raw_store.get(record.raw_object_key)
    except FileNotFoundError:
        base.detail = "raw payload missing (may have been evicted)"
        return base
    except Exception as exc:  # noqa: BLE001
        base.detail = f"raw payload unavailable: {exc}"
        return base

    primary_response = payload.get("primary_response")
    candidate_response = payload.get("candidate_response")
    base.primary_response = primary_response if isinstance(primary_response, dict) else None
    base.candidate_response = (
        candidate_response if isinstance(candidate_response, dict) else None
    )
    base.primary_content = _extract_message_content(base.primary_response)
    base.candidate_content = _extract_message_content(base.candidate_response)
    return base


class MetricsOut(BaseModel):
    """Real-time business metrics summary for /v1/metrics."""

    requests_total: int
    requests_success: int
    requests_error: int
    shadow_enqueued: int
    shadow_sampled_out: int
    shadow_errors: int
    shadow_timeouts: int
    verdict_match: int
    verdict_mismatch: int
    verdict_invalid_json: int
    exact_match_rate_pct: float
    shadow_sample_rate: float


@router.get("/metrics", response_model=MetricsOut)
async def business_metrics(request: Request) -> MetricsOut:
    """Real-time counters: total requests, shadow errors/timeouts, match %."""
    c = request.app.state.rt_counters
    finalized = c["verdict_match"] + c["verdict_mismatch"] + c["verdict_invalid_json"]
    pct = (100.0 * c["verdict_match"] / finalized) if finalized else 0.0
    return MetricsOut(
        requests_total=c["requests_total"],
        requests_success=c["requests_success"],
        requests_error=c["requests_error"],
        shadow_enqueued=c["shadow_enqueued"],
        shadow_sampled_out=c["shadow_sampled_out"],
        shadow_errors=c["shadow_errors"],
        shadow_timeouts=c["shadow_timeouts"],
        verdict_match=c["verdict_match"],
        verdict_mismatch=c["verdict_mismatch"],
        verdict_invalid_json=c["verdict_invalid_json"],
        exact_match_rate_pct=round(pct, 2),
        shadow_sample_rate=float(request.app.state.shadow_sample_rate),
    )


class ConfigPatch(BaseModel):
    """Runtime-mutable config knobs. All fields optional."""

    shadow_sample_rate: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Fraction of traffic to mirror to the candidate LLM. 1.0 = 100%.",
    )


@router.put("/config")
async def put_runtime_config(patch: ConfigPatch, request: Request) -> dict[str, Any]:
    """Runtime updates to shadow routing. e.g. flip mirror % from 100% to 50%."""
    applied: dict[str, Any] = {}
    if patch.shadow_sample_rate is not None:
        request.app.state.shadow_sample_rate = float(patch.shadow_sample_rate)
        applied["shadow_sample_rate"] = request.app.state.shadow_sample_rate
    return {"applied": applied, "current_shadow_sample_rate": request.app.state.shadow_sample_rate}


@router.get("/config", response_model=ConfigOut)
async def get_runtime_config(
    request: Request,
    config: AppConfig = Depends(get_config),
    settings: Settings = Depends(get_settings),
) -> ConfigOut:
    return ConfigOut(
        routes={
            name: {
                "primary": r.primary.model_dump(),
                "candidate": r.candidate.model_dump(),
            }
            for name, r in config.routes.items()
        },
        evaluator=config.evaluator.model_dump(),
        dispatcher=config.dispatcher.model_dump(),
        store=config.store.model_dump(),
        env_file=settings.env_file_path(),
        do_inference_base_url=settings.do_inference_base_url,
        do_inference_key=settings.redacted_key_summary(),
    )


# Also add the live shadow_sample_rate to /v1/config for observability.
# (Consumers already fetch this endpoint; adding a field is backward compatible.)
