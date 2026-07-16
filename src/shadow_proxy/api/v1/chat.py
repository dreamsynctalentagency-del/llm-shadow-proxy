"""POST /v1/chat — customer-facing primary chat proxy.

Contract:

- Return the primary LLM's response as fast as possible.
- Persist a durable ``ComparisonRecord`` **before** returning to the client, so
  the primary response is never lost.
- Fire-and-forget enqueue of the candidate work.
- Never fail the client on candidate errors or queue-full conditions.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from structlog.contextvars import bind_contextvars, clear_contextvars

from shadow_proxy.api.deps import (
    ApiKeyDep,
    get_config,
    get_dispatcher,
    get_metrics,
    get_primary_client,
    get_raw_store,
    get_settings,
    get_store,
)
from shadow_proxy.dispatcher import CandidateDispatcher, CandidateJob, DispatchStatus
from shadow_proxy.llm import ChatMessage, ChatRequest, LLMClient
from shadow_proxy.observability import Metrics, get_logger
from shadow_proxy.pipeline import persist_pending, run_primary
from shadow_proxy.settings import AppConfig, Settings
from shadow_proxy.store import ComparisonStore, PrimaryStatus, RawStore
from shadow_proxy.util.hashing import request_hash
from shadow_proxy.util.ids import new_request_id

_log = get_logger("shadow_proxy.api.chat")

router = APIRouter(prefix="/v1", tags=["chat"], dependencies=[ApiKeyDep])


async def _shadow_gate(
    request: Request,
    dispatcher: CandidateDispatcher,
    request_id: str,
) -> tuple[bool, bool, float]:
    """Sampling gate for shadow traffic. Returns (sampled_in, enqueued, rate).

    Sample rate is read live from ``app.state.shadow_sample_rate`` so PUT
    /v1/config takes effect immediately without a restart.
    """
    sample_rate = float(getattr(request.app.state, "shadow_sample_rate", 1.0))
    sampled_in = random.random() < sample_rate
    counters = getattr(request.app.state, "rt_counters", None)

    enqueued = False
    if sampled_in:
        dispatch = await dispatcher.enqueue(CandidateJob(request_id=request_id))
        enqueued = dispatch == DispatchStatus.ENQUEUED

    if counters is not None:
        counters["requests_total"] += 1
        counters["requests_success"] += 1
        if enqueued:
            counters["shadow_enqueued"] += 1
        elif not sampled_in:
            counters["shadow_sampled_out"] += 1

    return sampled_in, enqueued, sample_rate


class ChatMessageIn(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class ChatMetadata(BaseModel):
    tenant_id: str | None = None
    session_id: str | None = None


class ChatIn(BaseModel):
    messages: list[ChatMessageIn] = Field(..., min_length=1)
    temperature: float | None = None
    max_completion_tokens: int | None = Field(default=None, ge=1, le=32_768)
    metadata: ChatMetadata | None = None
    route: str = "default"


class ChatOut(BaseModel):
    request_id: str
    primary_model: str
    response: dict[str, Any]


@router.post("/chat", response_model=ChatOut, status_code=status.HTTP_200_OK)
async def chat(  # noqa: PLR0913 - fine here, all deps
    body: ChatIn,
    response: Response,
    request: Request,
    x_request_id: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),
    config: AppConfig = Depends(get_config),
    metrics: Metrics = Depends(get_metrics),
    store: ComparisonStore = Depends(get_store),
    raw_store: RawStore | None = Depends(get_raw_store),
    primary_client: LLMClient = Depends(get_primary_client),
    dispatcher: CandidateDispatcher = Depends(get_dispatcher),
) -> ChatOut:
    _ = settings  # currently unused directly (auth handled via dependency)
    _ = request

    request_id = x_request_id or new_request_id()
    received_at = datetime.now(UTC)

    try:
        route_cfg = config.route(body.route)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown route: {body.route}",
        ) from None

    tenant_id = body.metadata.tenant_id if body.metadata else None
    session_id = body.metadata.session_id if body.metadata else None

    bind_contextvars(request_id=request_id, route=body.route, tenant_id=tenant_id)

    try:
        request_body_dict = body.model_dump()
        req_hash = request_hash(request_body_dict)

        primary_req = ChatRequest(
            model=route_cfg.primary.model_id,
            messages=[ChatMessage(**m.model_dump()) for m in body.messages],
            temperature=body.temperature,
            max_completion_tokens=body.max_completion_tokens,
        )

        outcome = await run_primary(
            primary_client,
            req=primary_req,
            timeout_s=route_cfg.primary.timeout_s,
            route=body.route,
            metrics=metrics,
        )

        # Persist durable row (candidate=pending) BEFORE returning.
        await persist_pending(
            store=store,
            raw_store=raw_store,
            request_id=request_id,
            received_at=received_at,
            route=body.route,
            tenant_id=tenant_id,
            session_id=session_id,
            request_hash=req_hash,
            request_body=request_body_dict,
            primary_outcome=outcome,
            primary_model=route_cfg.primary.model_id,
            candidate_model=route_cfg.candidate.model_id,
            on_failure=config.store.on_failure,
            metrics=metrics,
        )

        if outcome.status == PrimaryStatus.TIMEOUT:
            metrics.requests_total.labels(route=body.route, status="504").inc()
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=f"Primary LLM timed out: {outcome.error}",
            )
        if outcome.status == PrimaryStatus.ERROR:
            code = outcome.error_status_code or status.HTTP_502_BAD_GATEWAY
            if code in (401, 403):
                metrics.requests_total.labels(route=body.route, status=str(code)).inc()
                raise HTTPException(
                    status_code=code, detail="Upstream authentication failed"
                )
            metrics.requests_total.labels(route=body.route, status="502").inc()
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Primary LLM failed: {outcome.error}",
            )

        sampled_in, enqueued, sample_rate = await _shadow_gate(
            request, dispatcher, request_id
        )

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Primary-Model"] = route_cfg.primary.model_id
        response.headers["X-Shadow-Enqueued"] = "true" if enqueued else "false"
        response.headers["X-Shadow-Sampled"] = "true" if sampled_in else "false"
        response.headers["X-Shadow-Sample-Rate"] = f"{sample_rate:.3f}"

        metrics.requests_total.labels(route=body.route, status="200").inc()

        return ChatOut(
            request_id=request_id,
            primary_model=route_cfg.primary.model_id,
            response=outcome.raw or {},
        )
    finally:
        clear_contextvars()
