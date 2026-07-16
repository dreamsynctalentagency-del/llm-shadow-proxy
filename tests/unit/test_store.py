from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shadow_proxy.evaluator import Verdict
from shadow_proxy.store import (
    CandidateStatus,
    EvaluationFilters,
    FinalizePatch,
    PendingComparison,
    PrimaryStatus,
    SqlComparisonStore,
)


@pytest.fixture()
async def store(tmp_path):  # type: ignore[no-untyped-def]
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    s = SqlComparisonStore(db_url)
    await s.initialize()
    try:
        yield s
    finally:
        await s.aclose()


def _make_pending(request_id: str = "01ID") -> PendingComparison:
    return PendingComparison(
        request_id=request_id,
        received_at=datetime.now(UTC),
        route="default",
        tenant_id="acme",
        session_id=None,
        request_hash="deadbeef",
        raw_object_key="raw/x.json.gz",
        primary_model="primary-model",
        primary_status=PrimaryStatus.OK,
        primary_latency_ms=200,
        primary_action="book_flight",
        primary_error=None,
        candidate_model="candidate-model",
    )


@pytest.mark.asyncio()
async def test_insert_pending_and_get(store: SqlComparisonStore) -> None:
    await store.insert_pending(_make_pending("rid1"))
    got = await store.get("rid1")
    assert got is not None
    assert got.request_id == "rid1"
    assert got.candidate_status is CandidateStatus.PENDING
    assert got.verdict is Verdict.PENDING


@pytest.mark.asyncio()
async def test_finalize_success(store: SqlComparisonStore) -> None:
    await store.insert_pending(_make_pending("rid2"))
    await store.mark_in_progress("rid2")
    await store.finalize(
        "rid2",
        FinalizePatch(
            candidate_status=CandidateStatus.OK,
            verdict=Verdict.MATCH,
            reasons=["action_match"],
            candidate_latency_ms=500,
            candidate_action="book_flight",
            evaluated_at=datetime.now(UTC),
        ),
    )
    got = await store.get("rid2")
    assert got is not None
    assert got.candidate_status is CandidateStatus.OK
    assert got.verdict is Verdict.MATCH
    assert got.candidate_latency_ms == 500


@pytest.mark.asyncio()
async def test_find_stale(store: SqlComparisonStore) -> None:
    old = _make_pending("old")
    old.received_at = datetime.now(UTC) - timedelta(hours=1)
    fresh = _make_pending("fresh")
    await store.insert_pending(old)
    await store.insert_pending(fresh)
    stale = await store.find_stale(timedelta(minutes=30))
    assert "old" in stale
    assert "fresh" not in stale


@pytest.mark.asyncio()
async def test_list_and_filter(store: SqlComparisonStore) -> None:
    for i in range(3):
        p = _make_pending(f"r{i}")
        await store.insert_pending(p)
    got = await store.list(EvaluationFilters(limit=10))
    assert len(got) == 3


@pytest.mark.asyncio()
async def test_summary(store: SqlComparisonStore) -> None:
    for i in range(5):
        p = _make_pending(f"s{i}")
        await store.insert_pending(p)
    # Finalize a mix
    await store.finalize(
        "s0",
        FinalizePatch(
            candidate_status=CandidateStatus.OK,
            verdict=Verdict.MATCH,
            reasons=[],
            candidate_latency_ms=100,
        ),
    )
    await store.finalize(
        "s1",
        FinalizePatch(
            candidate_status=CandidateStatus.OK,
            verdict=Verdict.MISMATCH,
            reasons=[],
            candidate_latency_ms=200,
        ),
    )
    summary = await store.summary(timedelta(hours=1))
    assert summary.total == 5
    assert summary.verdicts.get("match") == 1
    assert summary.verdicts.get("mismatch") == 1
    assert 0.0 < summary.match_rate <= 1.0
