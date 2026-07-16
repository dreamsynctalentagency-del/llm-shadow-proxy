"""End-to-end test of the FastAPI app with in-memory fakes.

Boots the real app factory but swaps the DO inference client with a
:class:`FakeLLMClient` and points the store at a per-test SQLite file.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from shadow_proxy.dispatcher import InProcessDispatcher
from shadow_proxy.evaluator import JsonActionEvaluator
from shadow_proxy.llm import ChatRequest, ChatResponse
from shadow_proxy.observability import Metrics, configure_logging
from shadow_proxy.pipeline import CandidateHandler
from shadow_proxy.settings import (
    AppConfig,
    DispatcherConfig,
    EvaluatorConfig,
    LLMEndpointConfig,
    RouteConfig,
    Settings,
    StoreConfig,
)
from shadow_proxy.store import FilesystemRawStore, SqlComparisonStore
from shadow_proxy.sweeper import Sweeper


class FakeLLMClient:
    def __init__(self, content: str, *, model: str = "fake", delay_s: float = 0.0) -> None:
        self._content = content
        self._model = model
        self._delay_s = delay_s
        self.calls: list[ChatRequest] = []

    async def chat_completions(self, req: ChatRequest, *, timeout_s: float) -> ChatResponse:  # noqa: ARG002
        self.calls.append(req)
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        raw = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "model": self._model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": self._content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        return ChatResponse(
            id="chatcmpl-fake",
            model=self._model,
            content=self._content,
            finish_reason="stop",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            raw=raw,
        )

    async def aclose(self) -> None:
        return None


def _test_config() -> AppConfig:
    return AppConfig(
        routes={
            "default": RouteConfig(
                primary=LLMEndpointConfig(model_id="primary-fake", timeout_s=5.0),
                candidate=LLMEndpointConfig(model_id="candidate-fake", timeout_s=5.0),
            )
        },
        evaluator=EvaluatorConfig(),
        dispatcher=DispatcherConfig(queue_capacity=100, workers=2),
        store=StoreConfig(sweeper_interval_s=60.0, stale_threshold_s=120.0),
    )


@pytest.fixture()
async def app_with_fakes(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Yield (app, primary_client, candidate_client, store)."""
    from fastapi import FastAPI

    from shadow_proxy.api import health as health_router_mod
    from shadow_proxy.api.v1 import chat as chat_router_mod
    from shadow_proxy.api.v1 import evaluations as evals_router_mod
    from shadow_proxy.observability.metrics import metrics_router

    configure_logging(level="WARNING", fmt="console")

    settings = Settings(
        do_inference_api_key="fake-key",
        proxy_api_keys="",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        raw_store_type="filesystem",
        raw_store_filesystem_path=str(tmp_path / "raw"),
    )
    config = _test_config()
    metrics = Metrics()

    store = SqlComparisonStore(settings.database_url)
    await store.initialize()
    raw_store = FilesystemRawStore(settings.raw_store_filesystem_path)

    primary = FakeLLMClient('{"action": "book_flight"}', model="primary-fake")
    candidate = FakeLLMClient('{"action": "book_flight"}', model="candidate-fake")

    evaluator = JsonActionEvaluator()
    dispatcher = InProcessDispatcher(capacity=100, workers=2, metrics=metrics)
    handler = CandidateHandler(
        candidate_client=candidate,
        store=store,
        raw_store=raw_store,
        evaluator=evaluator,
        route_config=config.route("default"),
        route_name="default",
        metrics=metrics,
    )
    await dispatcher.start(handler)
    sweeper = Sweeper(
        store=store,
        interval_s=config.store.sweeper_interval_s,
        stale_threshold_s=config.store.stale_threshold_s,
        metrics=metrics,
    )

    app = FastAPI()
    app.state.settings = settings
    app.state.config = config
    app.state.metrics = metrics
    app.state.store = store
    app.state.raw_store = raw_store
    app.state.primary_client = primary
    app.state.candidate_client = candidate
    app.state.evaluator = evaluator
    app.state.dispatcher = dispatcher
    app.state.sweeper = sweeper

    app.include_router(chat_router_mod.router)
    app.include_router(evals_router_mod.router)
    app.include_router(health_router_mod.router)
    app.include_router(metrics_router())

    try:
        yield app, primary, candidate, store, dispatcher
    finally:
        await dispatcher.stop(drain=True)
        await store.aclose()


async def _wait_for_finalize(
    store: SqlComparisonStore, request_id: str, *, timeout: float = 3.0
) -> None:
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        rec = await store.get(request_id)
        if rec is not None and rec.candidate_status.value not in ("pending", "in_progress"):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"Record {request_id} not finalized after {timeout}s")


@pytest.mark.asyncio()
async def test_chat_happy_path_match(app_with_fakes) -> None:  # type: ignore[no-untyped-def]
    app, _primary, _candidate, store, _dispatcher = app_with_fakes
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/chat",
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "temperature": 0.0,
            },
        )
    assert r.status_code == 200
    body = r.json()
    request_id = body["request_id"]
    assert body["primary_model"] == "primary-fake"
    assert body["response"]["choices"][0]["message"]["content"] == '{"action": "book_flight"}'
    assert r.headers["X-Shadow-Enqueued"] == "true"

    await _wait_for_finalize(store, request_id)
    record = await store.get(request_id)
    assert record is not None
    assert record.verdict.value == "match"
    assert record.candidate_status.value == "ok"
    assert record.primary_action == "book_flight"
    assert record.candidate_action == "book_flight"


@pytest.mark.asyncio()
async def test_chat_mismatch(tmp_path: Path) -> None:
    """Different action -> verdict mismatch, but client still gets 200."""
    from fastapi import FastAPI

    from shadow_proxy.api import health as health_router_mod
    from shadow_proxy.api.v1 import chat as chat_router_mod
    from shadow_proxy.api.v1 import evaluations as evals_router_mod
    from shadow_proxy.observability.metrics import metrics_router

    settings = Settings(
        do_inference_api_key="fake-key",
        proxy_api_keys="",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        raw_store_type="filesystem",
        raw_store_filesystem_path=str(tmp_path / "raw"),
    )
    config = _test_config()
    metrics = Metrics()

    store = SqlComparisonStore(settings.database_url)
    await store.initialize()
    raw_store = FilesystemRawStore(settings.raw_store_filesystem_path)
    primary = FakeLLMClient('{"action": "book_flight"}')
    candidate = FakeLLMClient('{"action": "cancel_flight"}')

    evaluator = JsonActionEvaluator()
    dispatcher = InProcessDispatcher(capacity=100, workers=2, metrics=metrics)
    await dispatcher.start(
        CandidateHandler(
            candidate_client=candidate,
            store=store,
            raw_store=raw_store,
            evaluator=evaluator,
            route_config=config.route("default"),
            route_name="default",
            metrics=metrics,
        )
    )

    app = FastAPI()
    app.state.settings = settings
    app.state.config = config
    app.state.metrics = metrics
    app.state.store = store
    app.state.raw_store = raw_store
    app.state.primary_client = primary
    app.state.candidate_client = candidate
    app.state.evaluator = evaluator
    app.state.dispatcher = dispatcher
    app.include_router(chat_router_mod.router)
    app.include_router(evals_router_mod.router)
    app.include_router(health_router_mod.router)
    app.include_router(metrics_router())

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/v1/chat",
                json={"messages": [{"role": "user", "content": "hi"}]},
            )
        assert r.status_code == 200
        rid = r.json()["request_id"]
        await _wait_for_finalize(store, rid)
        rec = await store.get(rid)
        assert rec is not None
        assert rec.verdict.value == "mismatch"
        assert rec.candidate_action == "cancel_flight"
    finally:
        await dispatcher.stop(drain=True)
        await store.aclose()


@pytest.mark.asyncio()
async def test_evaluations_summary_endpoint(app_with_fakes) -> None:  # type: ignore[no-untyped-def]
    app, _primary, _candidate, store, _dispatcher = app_with_fakes
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(3):
            r = await client.post(
                "/v1/chat",
                json={"messages": [{"role": "user", "content": "hi"}]},
            )
            assert r.status_code == 200
            await _wait_for_finalize(store, r.json()["request_id"])

        s = await client.get("/v1/evaluations/summary")
    assert s.status_code == 200
    data = s.json()
    assert data["total"] >= 3
    assert data["verdicts"].get("match", 0) >= 3


@pytest.mark.asyncio()
async def test_raw_endpoint_returns_both_responses(app_with_fakes) -> None:  # type: ignore[no-untyped-def]
    """/v1/evaluations/{id}/raw returns primary + candidate content and payloads."""
    app, _primary, _candidate, store, _dispatcher = app_with_fakes
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200
        request_id = r.json()["request_id"]
        await _wait_for_finalize(store, request_id)

        raw_r = await client.get(f"/v1/evaluations/{request_id}/raw")
    assert raw_r.status_code == 200
    body = raw_r.json()
    assert body["request_id"] == request_id
    assert body["primary_content"] == '{"action": "book_flight"}'
    assert body["candidate_content"] == '{"action": "book_flight"}'
    assert body["primary_model"] == "primary-fake"
    assert body["candidate_model"] == "candidate-fake"
    # Full raw payloads survive the round trip through the filesystem store
    assert body["primary_response"]["choices"][0]["message"]["content"] == (
        '{"action": "book_flight"}'
    )
    assert body["candidate_response"]["choices"][0]["message"]["content"] == (
        '{"action": "book_flight"}'
    )
    assert body["detail"] is None


@pytest.mark.asyncio()
async def test_raw_endpoint_missing_id_returns_404(app_with_fakes) -> None:  # type: ignore[no-untyped-def]
    app, *_ = app_with_fakes
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/v1/evaluations/does-not-exist/raw")
    assert r.status_code == 404
