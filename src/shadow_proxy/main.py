"""FastAPI app factory + lifespan wiring.

Boots the store, dispatcher, sweeper, and both LLM clients on startup; tears
them down cleanly on shutdown. Everything is attached to ``app.state`` so
routers can pick things up via ``Depends``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from shadow_proxy.api import health as health_router_mod
from shadow_proxy.api.v1 import chat as chat_router_mod
from shadow_proxy.api.v1 import evaluations as evals_router_mod
from shadow_proxy.dispatcher import InProcessDispatcher
from shadow_proxy.evaluator import JsonActionEvaluator
from shadow_proxy.llm import DOInferenceClient
from shadow_proxy.observability import Metrics, configure_logging, get_logger
from shadow_proxy.observability.metrics import metrics_router
from shadow_proxy.pipeline import CandidateHandler
from shadow_proxy.settings import Settings, get_settings, load_app_config
from shadow_proxy.store import SqlComparisonStore, build_raw_store
from shadow_proxy.store.mismatch_tape import MismatchTape
from shadow_proxy.sweeper import Sweeper, requeue_all_unfinished


def _ensure_local_dirs(settings: Settings, db_url: str) -> None:
    """Create parent dirs for SQLite / filesystem raw store so first-boot works."""
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if db_url.startswith(prefix):
            Path(db_url[len(prefix):]).parent.mkdir(parents=True, exist_ok=True)
            break
    if settings.raw_store_type == "filesystem":
        settings.resolved_raw_store_path().mkdir(parents=True, exist_ok=True)


def _new_counters() -> dict[str, int]:
    return {
        "requests_total": 0,
        "requests_success": 0,
        "requests_error": 0,
        "shadow_enqueued": 0,
        "shadow_sampled_out": 0,
        "shadow_errors": 0,
        "shadow_timeouts": 0,
        "verdict_match": 0,
        "verdict_mismatch": 0,
        "verdict_invalid_json": 0,
    }


def _mismatch_tape_path(settings: Settings, db_url: str) -> Path:
    """Co-locate mismatches.sqlite with the main SQLite DB when possible."""
    if db_url.startswith("sqlite+aiosqlite:///"):
        return Path(db_url[len("sqlite+aiosqlite:///"):]).parent / "mismatches.sqlite"
    return settings.resolved_raw_store_path().parent / "mismatches.sqlite"


def _build_raw_store(settings: Settings):  # type: ignore[no-untyped-def]
    if settings.raw_store_type == "spaces" and not settings.do_spaces_bucket:
        return None
    return build_raw_store(
        kind=settings.raw_store_type,
        filesystem_path=str(settings.resolved_raw_store_path()),
        bucket=settings.do_spaces_bucket,
        endpoint_url=settings.do_spaces_endpoint_url,
        region=settings.do_spaces_region,
        access_key=settings.do_spaces_key,
        secret_key=settings.do_spaces_secret.get_secret_value(),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # noqa: PLR0915 — wiring is intentionally linear
    settings: Settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    log = get_logger("shadow_proxy.main")

    # Fail loud & early on any missing/placeholder secrets. This is what
    # guarantees we NEVER send a dummy DO_INFERENCE_API_KEY to DigitalOcean.
    settings.assert_runtime_ready()

    config = load_app_config(settings.resolved_config_file())
    route = config.route("default")  # default route must exist

    metrics = Metrics()

    db_url = settings.resolved_database_url()
    _ensure_local_dirs(settings, db_url)

    store = SqlComparisonStore(db_url)
    await store.initialize()
    raw_store = _build_raw_store(settings)

    do_api_key = settings.do_inference_api_key.get_secret_value()
    strict_key = not settings.shadow_proxy_allow_dummy_key

    # Two separate clients so candidate saturation cannot starve primary.
    primary_client = DOInferenceClient(
        base_url=settings.do_inference_base_url,
        api_key=do_api_key,
        max_retries=route.primary.max_retries,
        strict=strict_key,
    )
    candidate_client = DOInferenceClient(
        base_url=settings.do_inference_base_url,
        api_key=do_api_key,
        max_retries=route.candidate.max_retries,
        strict=strict_key,
    )

    evaluator = JsonActionEvaluator(
        compare_key=config.evaluator.compare_key,
        require_json=config.evaluator.require_json,
        normalize=config.evaluator.normalize,
    )

    dispatcher = InProcessDispatcher(
        capacity=config.dispatcher.queue_capacity,
        workers=config.dispatcher.workers,
        overflow_policy=config.dispatcher.overflow_policy,
        metrics=metrics,
    )

    mismatch_tape = MismatchTape(_mismatch_tape_path(settings, db_url))
    await mismatch_tape.start()

    rt_counters: dict[str, int] = _new_counters()

    handler = CandidateHandler(
        candidate_client=candidate_client,
        store=store,
        raw_store=raw_store,
        evaluator=evaluator,
        route_config=route,
        route_name="default",
        metrics=metrics,
        mismatch_tape=mismatch_tape,
        counters=rt_counters,
    )
    await dispatcher.start(handler)

    sweeper = Sweeper(
        store=store,
        interval_s=config.store.sweeper_interval_s,
        stale_threshold_s=config.store.stale_threshold_s,
        metrics=metrics,
    )
    await sweeper.start()

    # Startup reconciliation: re-inject any rows still pending from a prior crash.
    requeued = await requeue_all_unfinished(store, dispatcher)
    if requeued:
        log.info("startup.requeued", count=requeued)

    app.state.settings = settings
    app.state.config = config
    app.state.metrics = metrics
    app.state.store = store
    app.state.raw_store = raw_store
    app.state.primary_client = primary_client
    app.state.candidate_client = candidate_client
    app.state.evaluator = evaluator
    app.state.dispatcher = dispatcher
    app.state.sweeper = sweeper
    app.state.mismatch_tape = mismatch_tape
    app.state.shadow_sample_rate = 1.0  # mutable via PUT /v1/config
    app.state.rt_counters = rt_counters

    log.info(
        "app.started",
        primary_model=route.primary.model_id,
        candidate_model=route.candidate.model_id,
        workers=config.dispatcher.workers,
        queue_capacity=config.dispatcher.queue_capacity,
        do_inference_base_url=settings.do_inference_base_url,
        do_inference_key=settings.redacted_key_summary(),
        env_file=settings.env_file_path() or "(none — using process env only)",
        allow_dummy_key=settings.shadow_proxy_allow_dummy_key,
    )
    try:
        yield
    finally:
        log.info("app.stopping")
        await sweeper.stop()
        await dispatcher.stop(drain=True)
        await mismatch_tape.aclose()
        await primary_client.aclose()
        await candidate_client.aclose()
        if raw_store is not None:
            await raw_store.aclose()
        await store.aclose()
        log.info("app.stopped")


def _resolve_ui_dir() -> Path | None:
    """Locate the bundled test-console UI.

    Searches (in order):

    - ``$UI_DIR`` env var override
    - ``<repo_root>/ui`` (developer machine layout)
    - ``/app/ui`` (Docker layout — see docker/Dockerfile)
    """
    import os

    override = os.environ.get("UI_DIR")
    candidates = []
    if override:
        candidates.append(Path(override))
    here = Path(__file__).resolve()
    candidates.append(here.parents[2] / "ui")
    candidates.append(Path("/app/ui"))
    for c in candidates:
        if c.is_dir() and (c / "index.html").is_file():
            return c
    return None


def create_app() -> FastAPI:
    app = FastAPI(
        title="LLM Shadow Proxy",
        version="0.1.0",
        description=(
            "Serves customer traffic through a Primary LLM while shadowing "
            "the same traffic to a Candidate LLM and evaluating both responses."
        ),
        lifespan=lifespan,
    )
    app.include_router(chat_router_mod.router)
    app.include_router(evals_router_mod.router)
    app.include_router(health_router_mod.router)
    app.include_router(metrics_router())

    ui_dir = _resolve_ui_dir()
    if ui_dir is not None:
        app.mount("/ui", StaticFiles(directory=ui_dir, html=True), name="ui")

        @app.get("/", include_in_schema=False)
        async def _root() -> RedirectResponse:
            return RedirectResponse(url="/ui/")

    return app


app = create_app()
