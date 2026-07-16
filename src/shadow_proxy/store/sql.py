"""SQLAlchemy-based ComparisonStore.

Works transparently against SQLite (dev, ``sqlite+aiosqlite://``) and Postgres
(prod, ``postgresql+asyncpg://``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from shadow_proxy.evaluator import Verdict
from shadow_proxy.store.base import (
    CandidateStatus,
    ComparisonRecord,
    EvaluationFilters,
    FinalizePatch,
    PendingComparison,
    PrimaryStatus,
    SummaryStats,
)
from shadow_proxy.store.models import Base, ComparisonRow


class SqlComparisonStore:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._engine: AsyncEngine = create_async_engine(
            database_url,
            future=True,
            pool_pre_ping=True,
        )
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def initialize(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def aclose(self) -> None:
        await self._engine.dispose()

    async def insert_pending(self, record: PendingComparison) -> None:
        row = ComparisonRow(
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
            candidate_status=CandidateStatus.PENDING.value,
            verdict=(
                Verdict.PRIMARY_ERROR.value
                if record.primary_status != PrimaryStatus.OK
                else Verdict.PENDING.value
            ),
            reasons=[],
        )
        async with self._session() as sess:
            sess.add(row)
            await sess.commit()

    async def mark_in_progress(self, request_id: str) -> None:
        now = datetime.now(UTC)
        async with self._session() as sess:
            await sess.execute(
                update(ComparisonRow)
                .where(ComparisonRow.request_id == request_id)
                .values(
                    candidate_status=CandidateStatus.IN_PROGRESS.value,
                    last_attempted_at=now,
                    attempt_count=ComparisonRow.attempt_count + 1,
                )
            )
            await sess.commit()

    async def finalize(self, request_id: str, patch: FinalizePatch) -> None:
        values: dict[str, Any] = {
            "candidate_status": patch.candidate_status.value,
            "verdict": patch.verdict.value,
            "reasons": patch.reasons,
            "evaluated_at": patch.evaluated_at or datetime.now(UTC),
        }
        if patch.candidate_latency_ms is not None:
            values["candidate_latency_ms"] = patch.candidate_latency_ms
        if patch.candidate_action is not None:
            values["candidate_action"] = patch.candidate_action
        if patch.candidate_error is not None:
            values["candidate_error"] = patch.candidate_error
        if patch.last_attempted_at is not None:
            values["last_attempted_at"] = patch.last_attempted_at

        async with self._session() as sess:
            await sess.execute(
                update(ComparisonRow)
                .where(ComparisonRow.request_id == request_id)
                .values(**values)
            )
            await sess.commit()

    async def get(self, request_id: str) -> ComparisonRecord | None:
        async with self._session() as sess:
            row = await sess.get(ComparisonRow, request_id)
            if row is None:
                return None
            return _to_record(row)

    async def list(self, filters: EvaluationFilters) -> list[ComparisonRecord]:
        stmt = select(ComparisonRow)
        if filters.since is not None:
            stmt = stmt.where(ComparisonRow.received_at >= filters.since)
        if filters.until is not None:
            stmt = stmt.where(ComparisonRow.received_at <= filters.until)
        if filters.verdict is not None:
            stmt = stmt.where(ComparisonRow.verdict == filters.verdict.value)
        if filters.route is not None:
            stmt = stmt.where(ComparisonRow.route == filters.route)
        if filters.tenant_id is not None:
            stmt = stmt.where(ComparisonRow.tenant_id == filters.tenant_id)
        stmt = (
            stmt.order_by(ComparisonRow.received_at.desc())
            .limit(filters.limit)
            .offset(filters.offset)
        )
        async with self._session() as sess:
            result = await sess.execute(stmt)
            rows = result.scalars().all()
            return [_to_record(r) for r in rows]

    async def summary(self, window: timedelta) -> SummaryStats:
        since = datetime.now(UTC) - window
        async with self._session() as sess:
            total_res = await sess.execute(
                select(func.count()).select_from(ComparisonRow).where(
                    ComparisonRow.received_at >= since
                )
            )
            total = int(total_res.scalar_one() or 0)

            verdict_res = await sess.execute(
                select(ComparisonRow.verdict, func.count())
                .where(ComparisonRow.received_at >= since)
                .group_by(ComparisonRow.verdict)
            )
            verdicts: dict[str, int] = {v: int(c) for v, c in verdict_res.all()}

            lat_res = await sess.execute(
                select(
                    ComparisonRow.primary_latency_ms,
                    ComparisonRow.candidate_latency_ms,
                ).where(ComparisonRow.received_at >= since)
            )
            primary_lats: list[int] = []
            candidate_lats: list[int] = []
            for p_lat, c_lat in lat_res.all():
                if p_lat is not None:
                    primary_lats.append(int(p_lat))
                if c_lat is not None:
                    candidate_lats.append(int(c_lat))

        return SummaryStats(
            window_seconds=int(window.total_seconds()),
            total=total,
            verdicts=verdicts,
            primary_latency_p50_ms=_percentile(primary_lats, 0.50),
            primary_latency_p95_ms=_percentile(primary_lats, 0.95),
            candidate_latency_p50_ms=_percentile(candidate_lats, 0.50),
            candidate_latency_p95_ms=_percentile(candidate_lats, 0.95),
        )

    async def find_stale(
        self, threshold: timedelta, *, limit: int = 1000
    ) -> list[str]:
        cutoff = datetime.now(UTC) - threshold
        stmt = (
            select(ComparisonRow.request_id)
            .where(
                ComparisonRow.candidate_status.in_(
                    [CandidateStatus.PENDING.value, CandidateStatus.IN_PROGRESS.value]
                )
            )
            .where(ComparisonRow.received_at < cutoff)
            .limit(limit)
        )
        async with self._session() as sess:
            result = await sess.execute(stmt)
            return [cast(str, r) for r in result.scalars().all()]

    async def find_all_unfinished(self, *, limit: int = 10_000) -> list[str]:
        stmt = (
            select(ComparisonRow.request_id)
            .where(
                ComparisonRow.candidate_status.in_(
                    [CandidateStatus.PENDING.value, CandidateStatus.IN_PROGRESS.value]
                )
            )
            .order_by(ComparisonRow.received_at.asc())
            .limit(limit)
        )
        async with self._session() as sess:
            result = await sess.execute(stmt)
            return [cast(str, r) for r in result.scalars().all()]


def _percentile(values: list[int], q: float) -> float | None:
    if not values:
        return None
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    k = (len(sorted_values) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = k - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def _to_record(row: ComparisonRow) -> ComparisonRecord:
    reasons = row.reasons if isinstance(row.reasons, list) else []
    return ComparisonRecord(
        request_id=row.request_id,
        received_at=row.received_at,
        route=row.route,
        tenant_id=row.tenant_id,
        session_id=row.session_id,
        request_hash=row.request_hash,
        raw_object_key=row.raw_object_key,
        primary_model=row.primary_model,
        primary_status=PrimaryStatus(row.primary_status),
        primary_latency_ms=row.primary_latency_ms,
        primary_action=row.primary_action,
        primary_error=row.primary_error,
        candidate_model=row.candidate_model,
        candidate_status=CandidateStatus(row.candidate_status),
        candidate_latency_ms=row.candidate_latency_ms,
        candidate_action=row.candidate_action,
        candidate_error=row.candidate_error,
        verdict=Verdict(row.verdict),
        reasons=list(reasons),
        attempt_count=row.attempt_count,
        last_attempted_at=row.last_attempted_at,
        evaluated_at=row.evaluated_at,
    )
