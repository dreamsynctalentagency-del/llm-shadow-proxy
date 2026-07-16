"""End-to-end pipeline glue.

- ``run_primary`` performs the synchronous primary call and returns a
  :class:`PrimaryOutcome` including the LLM response and latency.
- ``persist_pending`` writes the initial row (candidate=pending) and archives
  the raw payload.
- ``handle_candidate`` is the async handler bound to each dispatcher worker.
  It fetches the row, calls the candidate LLM, evaluates, and finalizes the row.

All three functions are small, pure-ish, and independently testable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from shadow_proxy.dispatcher import CandidateJob
from shadow_proxy.evaluator import Evaluator, Verdict
from shadow_proxy.evaluator.json_action import _strip_code_fence  # pragma: no cover
from shadow_proxy.llm import ChatRequest, LLMCallError, LLMClient, LLMTimeout
from shadow_proxy.observability import Metrics, get_logger
from shadow_proxy.settings import RouteConfig
from shadow_proxy.store import (
    CandidateStatus,
    ComparisonStore,
    FinalizePatch,
    PendingComparison,
    PrimaryStatus,
    RawStore,
)

_log = get_logger("shadow_proxy.pipeline")


@dataclass(slots=True)
class PrimaryOutcome:
    status: PrimaryStatus
    latency_ms: int
    content: str = ""
    raw: dict[str, Any] | None = None
    error: str | None = None
    error_status_code: int | None = None


async def run_primary(
    client: LLMClient,
    *,
    req: ChatRequest,
    timeout_s: float,
    route: str,
    metrics: Metrics | None = None,
) -> PrimaryOutcome:
    start = time.perf_counter()
    try:
        resp = await client.chat_completions(req, timeout_s=timeout_s)
    except LLMTimeout as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        if metrics is not None:
            metrics.primary_latency.labels(
                route=route, model=req.model, outcome="timeout"
            ).observe(latency_ms / 1000)
        return PrimaryOutcome(
            status=PrimaryStatus.TIMEOUT,
            latency_ms=latency_ms,
            error=str(exc),
        )
    except LLMCallError as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        if metrics is not None:
            metrics.primary_latency.labels(
                route=route, model=req.model, outcome="error"
            ).observe(latency_ms / 1000)
        return PrimaryOutcome(
            status=PrimaryStatus.ERROR,
            latency_ms=latency_ms,
            error=str(exc),
            error_status_code=exc.status_code,
        )
    latency_ms = int((time.perf_counter() - start) * 1000)
    if metrics is not None:
        metrics.primary_latency.labels(
            route=route, model=req.model, outcome="ok"
        ).observe(latency_ms / 1000)
    return PrimaryOutcome(
        status=PrimaryStatus.OK,
        latency_ms=latency_ms,
        content=resp.content,
        raw=resp.raw,
    )


def extract_action(content: str, *, key: str = "action") -> str | None:
    """Best-effort extraction of ``key`` from a JSON-looking LLM response.

    Returns ``None`` when the content is not JSON or when the key is missing.
    Kept as a utility so the primary path can record ``primary_action`` up
    front, even before the candidate finishes.
    """
    import json

    stripped = _strip_code_fence(content or "")
    if not stripped:
        return None
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if isinstance(obj, dict):
        val = obj.get(key)
        return None if val is None else str(val)
    return None


async def persist_pending(
    *,
    store: ComparisonStore,
    raw_store: RawStore | None,
    request_id: str,
    received_at: datetime,
    route: str,
    tenant_id: str | None,
    session_id: str | None,
    request_hash: str,
    request_body: dict[str, Any],
    primary_outcome: PrimaryOutcome,
    primary_model: str,
    candidate_model: str,
    on_failure: str,
    metrics: Metrics | None = None,
) -> None:
    raw_object_key: str | None = None
    if raw_store is not None:
        try:
            payload = {
                "request": request_body,
                "primary_model": primary_model,
                "candidate_model": candidate_model,
                "primary_response": primary_outcome.raw,
                "candidate_response": None,
                "written_at": datetime.now(UTC).isoformat(),
            }
            raw_object_key = await raw_store.put(request_id, payload)
        except Exception as exc:  # noqa: BLE001
            _log.warning("raw_store.put_failed", error=str(exc), request_id=request_id)
            if metrics is not None:
                metrics.store_failures_total.labels(op="raw_put").inc()

    primary_action: str | None = None
    if primary_outcome.status == PrimaryStatus.OK:
        primary_action = extract_action(primary_outcome.content)

    record = PendingComparison(
        request_id=request_id,
        received_at=received_at,
        route=route,
        tenant_id=tenant_id,
        session_id=session_id,
        request_hash=request_hash,
        raw_object_key=raw_object_key,
        primary_model=primary_model,
        primary_status=primary_outcome.status,
        primary_latency_ms=primary_outcome.latency_ms,
        primary_action=primary_action,
        primary_error=primary_outcome.error,
        candidate_model=candidate_model,
    )
    try:
        await store.insert_pending(record)
    except Exception as exc:  # noqa: BLE001
        _log.error("store.insert_pending_failed", error=str(exc), request_id=request_id)
        if metrics is not None:
            metrics.store_failures_total.labels(op="insert_pending").inc()
        if on_failure == "fail_closed":
            raise


class CandidateHandler:
    """Bound handler used by the dispatcher's worker pool."""

    def __init__(
        self,
        *,
        candidate_client: LLMClient,
        store: ComparisonStore,
        raw_store: RawStore | None,
        evaluator: Evaluator,
        route_config: RouteConfig,
        route_name: str,
        metrics: Metrics | None = None,
        mismatch_tape: Any = None,
        counters: dict[str, int] | None = None,
    ) -> None:
        self._client = candidate_client
        self._store = store
        self._raw_store = raw_store
        self._evaluator = evaluator
        self._route = route_config
        self._route_name = route_name
        self._metrics = metrics
        self._mismatch_tape = mismatch_tape
        self._counters = counters

    async def __call__(self, job: CandidateJob) -> None:
        request_id = job.request_id
        record = await self._store.get(request_id)
        if record is None:
            _log.warning("candidate.record_missing", request_id=request_id)
            return
        if record.primary_status != PrimaryStatus.OK:
            # Nothing to evaluate against
            await self._store.finalize(
                request_id,
                FinalizePatch(
                    candidate_status=CandidateStatus.DROPPED,
                    verdict=Verdict.PRIMARY_ERROR,
                    reasons=["primary_error_no_shadow"],
                    evaluated_at=datetime.now(UTC),
                ),
            )
            return

        await self._store.mark_in_progress(request_id)

        # Rehydrate the request from the raw store (fallback: empty messages)
        request_body = await self._load_request_body(record.raw_object_key)
        req = ChatRequest(
            model=self._route.candidate.model_id,
            messages=request_body.get("request", {}).get("messages", []),
            temperature=request_body.get("request", {}).get("temperature"),
            max_completion_tokens=request_body.get("request", {}).get(
                "max_completion_tokens"
            ),
        )

        start = time.perf_counter()
        try:
            resp = await self._client.chat_completions(
                req, timeout_s=self._route.candidate.timeout_s
            )
        except LLMTimeout as exc:
            await self._finalize_error(
                request_id,
                error=f"timeout: {exc}",
                start=start,
                outcome_label="timeout",
                model_id=self._route.candidate.model_id,
            )
            return
        except LLMCallError as exc:
            await self._finalize_error(
                request_id,
                error=str(exc),
                start=start,
                outcome_label="error",
                model_id=self._route.candidate.model_id,
            )
            return

        latency_ms = int((time.perf_counter() - start) * 1000)
        if self._metrics is not None:
            self._metrics.candidate_latency.labels(
                route=self._route_name,
                model=self._route.candidate.model_id,
                outcome="ok",
            ).observe(latency_ms / 1000)

        primary_content = _primary_content_from_raw(request_body)
        result = self._evaluator.evaluate(primary_content, resp.content)

        # Update raw store with candidate response (best-effort)
        await self._merge_candidate_into_raw(
            record.raw_object_key, resp.raw
        )

        await self._store.finalize(
            request_id,
            FinalizePatch(
                candidate_status=CandidateStatus.OK,
                verdict=result.verdict,
                reasons=result.reasons,
                candidate_latency_ms=latency_ms,
                candidate_action=result.candidate_action,
                evaluated_at=datetime.now(UTC),
            ),
        )
        if self._metrics is not None:
            self._metrics.verdicts_total.labels(
                route=self._route_name, verdict=result.verdict.value
            ).inc()

        # Business counters for GET /v1/metrics
        if self._counters is not None:
            key = f"verdict_{result.verdict.value}"
            if key in ("verdict_match", "verdict_mismatch", "verdict_invalid_json"):
                self._counters[key] = self._counters.get(key, 0) + 1

        # Stream mismatched payloads onto the mismatch tape (SQLite) for
        # offline visualization. Fire-and-forget: never blocks the pipeline.
        if (
            self._mismatch_tape is not None
            and result.verdict.value in ("mismatch", "invalid_json")
        ):
            self._mismatch_tape.offer(
                {
                    "request_id": request_id,
                    "finalized_at": datetime.now(UTC).isoformat(),
                    "route": self._route_name,
                    "verdict": result.verdict.value,
                    "primary_model": self._route.primary.model_id,
                    "candidate_model": self._route.candidate.model_id,
                    "primary_action": record.primary_action,
                    "candidate_action": result.candidate_action,
                    "primary_content": primary_content,
                    "candidate_content": resp.content,
                    "primary_latency_ms": record.primary_latency_ms,
                    "candidate_latency_ms": latency_ms,
                    "reasons": result.reasons,
                    "request_body": request_body.get("request", {}),
                }
            )

    async def _finalize_error(
        self,
        request_id: str,
        *,
        error: str,
        start: float,
        outcome_label: str,
        model_id: str,
    ) -> None:
        latency_ms = int((time.perf_counter() - start) * 1000)
        if self._counters is not None:
            self._counters["shadow_errors"] = self._counters.get("shadow_errors", 0) + 1
            if outcome_label == "timeout":
                self._counters["shadow_timeouts"] = self._counters.get("shadow_timeouts", 0) + 1
        if self._metrics is not None:
            self._metrics.candidate_latency.labels(
                route=self._route_name, model=model_id, outcome=outcome_label
            ).observe(latency_ms / 1000)
            self._metrics.verdicts_total.labels(
                route=self._route_name, verdict=Verdict.CANDIDATE_ERROR.value
            ).inc()
        await self._store.finalize(
            request_id,
            FinalizePatch(
                candidate_status=CandidateStatus.ERROR,
                verdict=Verdict.CANDIDATE_ERROR,
                reasons=[f"candidate_{outcome_label}"],
                candidate_latency_ms=latency_ms,
                candidate_error=error[:1000],
                evaluated_at=datetime.now(UTC),
            ),
        )

    async def _load_request_body(self, object_key: str | None) -> dict[str, Any]:
        if not object_key or self._raw_store is None:
            return {}
        try:
            return await self._raw_store.get(object_key)
        except Exception as exc:  # noqa: BLE001
            _log.warning("raw_store.get_failed", error=str(exc), object_key=object_key)
            if self._metrics is not None:
                self._metrics.store_failures_total.labels(op="raw_get").inc()
            return {}

    async def _merge_candidate_into_raw(
        self, object_key: str | None, candidate_response: dict[str, Any]
    ) -> None:
        if not object_key or self._raw_store is None:
            return
        try:
            payload = await self._raw_store.get(object_key)
        except Exception:  # noqa: BLE001
            return
        payload["candidate_response"] = candidate_response
        # Reuse the same key by extracting request_id from the key path
        request_id = object_key.rsplit("/", 1)[-1].removesuffix(".json.gz")
        try:
            await self._raw_store.put(request_id, payload)
        except Exception as exc:  # noqa: BLE001
            _log.warning("raw_store.merge_failed", error=str(exc), object_key=object_key)


def _primary_content_from_raw(request_body: dict[str, Any]) -> str:
    resp = request_body.get("primary_response") or {}
    try:
        return resp["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""
