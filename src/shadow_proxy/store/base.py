"""ComparisonStore and RawStore protocols + shared value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from shadow_proxy.evaluator import Verdict


class PrimaryStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"


class CandidateStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    OK = "ok"
    ERROR = "error"
    TIMEOUT_STALE = "timeout_stale"
    DROPPED = "dropped"


@dataclass(slots=True)
class PendingComparison:
    """Row inserted immediately after the primary call completes."""

    request_id: str
    received_at: datetime
    route: str
    tenant_id: str | None
    session_id: str | None
    request_hash: str
    raw_object_key: str | None
    primary_model: str
    primary_status: PrimaryStatus
    primary_latency_ms: int
    primary_action: str | None
    primary_error: str | None
    candidate_model: str


@dataclass(slots=True)
class FinalizePatch:
    """Fields written when the candidate call finishes (success or failure)."""

    candidate_status: CandidateStatus
    verdict: Verdict
    reasons: list[str]
    candidate_latency_ms: int | None = None
    candidate_action: str | None = None
    candidate_error: str | None = None
    evaluated_at: datetime | None = None
    attempt_count: int = 1
    last_attempted_at: datetime | None = None


@dataclass(slots=True)
class ComparisonRecord:
    """Full row read from the store."""

    request_id: str
    received_at: datetime
    route: str
    tenant_id: str | None
    session_id: str | None
    request_hash: str
    raw_object_key: str | None

    primary_model: str
    primary_status: PrimaryStatus
    primary_latency_ms: int
    primary_action: str | None
    primary_error: str | None

    candidate_model: str
    candidate_status: CandidateStatus
    candidate_latency_ms: int | None
    candidate_action: str | None
    candidate_error: str | None

    verdict: Verdict
    reasons: list[str]
    attempt_count: int
    last_attempted_at: datetime | None
    evaluated_at: datetime | None


@dataclass(slots=True)
class EvaluationFilters:
    since: datetime | None = None
    until: datetime | None = None
    verdict: Verdict | None = None
    route: str | None = None
    tenant_id: str | None = None
    limit: int = 100
    offset: int = 0


@dataclass(slots=True)
class SummaryStats:
    window_seconds: int
    total: int
    verdicts: dict[str, int] = field(default_factory=dict)
    primary_latency_p50_ms: float | None = None
    primary_latency_p95_ms: float | None = None
    candidate_latency_p50_ms: float | None = None
    candidate_latency_p95_ms: float | None = None

    @property
    def match_rate(self) -> float:
        finalized = sum(
            self.verdicts.get(v, 0)
            for v in ("match", "mismatch", "invalid_json", "candidate_error")
        )
        if finalized == 0:
            return 0.0
        return self.verdicts.get("match", 0) / finalized


@runtime_checkable
class ComparisonStore(Protocol):
    async def initialize(self) -> None: ...

    async def insert_pending(self, record: PendingComparison) -> None: ...

    async def mark_in_progress(self, request_id: str) -> None: ...

    async def finalize(self, request_id: str, patch: FinalizePatch) -> None: ...

    async def get(self, request_id: str) -> ComparisonRecord | None: ...

    async def list(self, filters: EvaluationFilters) -> list[ComparisonRecord]: ...

    async def summary(self, window: timedelta) -> SummaryStats: ...

    async def find_stale(self, threshold: timedelta, *, limit: int = 1000) -> list[str]: ...

    async def find_all_unfinished(self, *, limit: int = 10_000) -> list[str]: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class RawStore(Protocol):
    async def put(self, request_id: str, payload: dict[str, Any]) -> str: ...

    async def get(self, object_key: str) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...
