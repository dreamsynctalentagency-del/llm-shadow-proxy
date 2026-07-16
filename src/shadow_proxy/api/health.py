"""Liveness and readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import text

from shadow_proxy.api.deps import get_store

router = APIRouter(tags=["ops"])


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
async def readyz(
    request: Request,
    _store=Depends(get_store),
) -> Response:
    engine = getattr(_store, "_engine", None)
    if engine is None:
        return Response("no store", status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        return Response("db unreachable", status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response("ready", status_code=status.HTTP_200_OK)
