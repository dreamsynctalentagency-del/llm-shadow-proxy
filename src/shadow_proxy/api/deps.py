"""Shared dependencies for the FastAPI routers.

All heavy singletons (LLM clients, store, dispatcher, evaluator, metrics) live
on ``app.state`` and are surfaced to routers via typed accessors.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Header, HTTPException, Request, status

if TYPE_CHECKING:  # pragma: no cover
    from shadow_proxy.dispatcher import CandidateDispatcher
    from shadow_proxy.evaluator import Evaluator
    from shadow_proxy.llm import LLMClient
    from shadow_proxy.observability import Metrics
    from shadow_proxy.settings import AppConfig, Settings
    from shadow_proxy.store import ComparisonStore, RawStore


def get_settings(request: Request) -> "Settings":
    return request.app.state.settings  # type: ignore[no-any-return]


def get_config(request: Request) -> "AppConfig":
    return request.app.state.config  # type: ignore[no-any-return]


def get_metrics(request: Request) -> "Metrics":
    return request.app.state.metrics  # type: ignore[no-any-return]


def get_store(request: Request) -> "ComparisonStore":
    return request.app.state.store  # type: ignore[no-any-return]


def get_raw_store(request: Request) -> "RawStore | None":
    return request.app.state.raw_store  # type: ignore[no-any-return]


def get_primary_client(request: Request) -> "LLMClient":
    return request.app.state.primary_client  # type: ignore[no-any-return]


def get_candidate_client(request: Request) -> "LLMClient":
    return request.app.state.candidate_client  # type: ignore[no-any-return]


def get_dispatcher(request: Request) -> "CandidateDispatcher":
    return request.app.state.dispatcher  # type: ignore[no-any-return]


def get_evaluator(request: Request) -> "Evaluator":
    return request.app.state.evaluator  # type: ignore[no-any-return]


async def require_api_key(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Simple bearer-token auth. Disabled when ``PROXY_API_KEYS`` is empty."""
    keys = request.app.state.settings.proxy_api_key_set
    if not keys:
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
        )
    presented = authorization.split(" ", 1)[1].strip()
    if presented not in keys:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid API key"
        )


ApiKeyDep = Depends(require_api_key)
