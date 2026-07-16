"""SQLAlchemy ORM models.

Uses the same schema on both SQLite (dev) and Postgres (prod). We rely on
SQLAlchemy's JSON type which maps to JSONB on Postgres and TEXT-encoded JSON on
SQLite. Timestamps are timezone-aware.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ComparisonRow(Base):
    __tablename__ = "comparisons"

    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    route: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'default'")
    )
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_object_key: Mapped[str | None] = mapped_column(String(256), nullable=True)

    primary_model: Mapped[str] = mapped_column(String(128), nullable=False)
    primary_status: Mapped[str] = mapped_column(String(32), nullable=False)
    primary_latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    primary_action: Mapped[str | None] = mapped_column(String(256), nullable=True)
    primary_error: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    candidate_model: Mapped[str] = mapped_column(String(128), nullable=False)
    candidate_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'pending'")
    )
    candidate_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    candidate_action: Mapped[str | None] = mapped_column(String(256), nullable=True)
    candidate_error: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    verdict: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'pending'")
    )
    reasons: Mapped[list[Any]] = mapped_column(
        JSON, nullable=False, server_default=text("'[]'")
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("idx_comparisons_received_at", "received_at"),
        Index("idx_comparisons_verdict", "verdict"),
        Index("idx_comparisons_route", "route"),
        Index("idx_comparisons_tenant", "tenant_id"),
        # SQLite supports partial indexes too
        Index(
            "idx_comparisons_pending",
            "received_at",
            sqlite_where=text("candidate_status IN ('pending','in_progress')"),
            postgresql_where=text("candidate_status IN ('pending','in_progress')"),
        ),
    )
