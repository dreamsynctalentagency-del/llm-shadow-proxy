"""Mismatch tape — a dedicated SQLite file for debugging & offline analysis.

Design:

- Separate SQLite file (default: ``./data/mismatches.sqlite``) so the OLTP
  ``comparisons`` DB stays clean and the tape can be shipped / rotated
  independently.
- Written via a single background writer task fed by an ``asyncio.Queue``, so
  the pipeline's finalize path never blocks on I/O.
- Bounded queue with drop-oldest policy: never applies backpressure to the
  candidate handler.
- Schema is intentionally denormalized so a single row is self-contained for
  visualization / grepping.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


class MismatchTape:
    """Async streaming writer for mismatched shadow comparisons."""

    def __init__(self, db_path: str | Path, *, queue_size: int = 1024) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_size)
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        # Initialize schema on the writer thread's connection (sqlite3 is sync
        # but we use a single dedicated task, so no lock contention).
        with sqlite3.connect(self._path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS mismatches (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id           TEXT    NOT NULL,
                    finalized_at         TEXT    NOT NULL,
                    route                TEXT    NOT NULL,
                    verdict              TEXT    NOT NULL,
                    primary_model        TEXT    NOT NULL,
                    candidate_model      TEXT    NOT NULL,
                    primary_action       TEXT,
                    candidate_action     TEXT,
                    primary_content      TEXT,
                    candidate_content    TEXT,
                    primary_latency_ms   INTEGER,
                    candidate_latency_ms INTEGER,
                    reasons              TEXT,
                    request_body         TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_mismatches_finalized_at
                    ON mismatches(finalized_at);
                CREATE INDEX IF NOT EXISTS idx_mismatches_request_id
                    ON mismatches(request_id);
                """
            )
        self._task = asyncio.create_task(self._writer_loop(), name="mismatch-tape")

    async def _writer_loop(self) -> None:
        # Single long-lived connection on this task. WAL for concurrent readers.
        conn = sqlite3.connect(self._path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            while not self._stopped.is_set():
                try:
                    row = await asyncio.wait_for(self._queue.get(), timeout=0.5)
                except TimeoutError:
                    continue
                if row is None:  # sentinel
                    break
                try:
                    conn.execute(
                        """
                        INSERT INTO mismatches (
                            request_id, finalized_at, route, verdict,
                            primary_model, candidate_model,
                            primary_action, candidate_action,
                            primary_content, candidate_content,
                            primary_latency_ms, candidate_latency_ms,
                            reasons, request_body
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            row["request_id"],
                            row["finalized_at"],
                            row["route"],
                            row["verdict"],
                            row["primary_model"],
                            row["candidate_model"],
                            row.get("primary_action"),
                            row.get("candidate_action"),
                            row.get("primary_content"),
                            row.get("candidate_content"),
                            row.get("primary_latency_ms"),
                            row.get("candidate_latency_ms"),
                            json.dumps(row.get("reasons") or []),
                            json.dumps(row.get("request_body") or {}, default=str),
                        ),
                    )
                    conn.commit()
                except Exception:  # noqa: BLE001
                    # Never let a single bad row kill the writer.
                    conn.rollback()
        finally:
            conn.close()

    def offer(self, row: dict[str, Any]) -> bool:
        """Non-blocking enqueue. Returns True if accepted, False on overflow."""
        try:
            self._queue.put_nowait(row)
            return True
        except asyncio.QueueFull:
            # Drop the oldest to make room for the newest — mismatches are
            # more useful "recent-heavy" than "old-heavy" for debugging.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(row)
                return True
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                return False

    async def aclose(self) -> None:
        self._stopped.set()
        try:
            self._queue.put_nowait(None)  # type: ignore[arg-type]
        except asyncio.QueueFull:
            pass
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
