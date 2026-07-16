"""CandidateDispatcher protocol.

Only the *request_id* travels through the queue. The worker fetches the full
row from the ComparisonStore, so the queue message is tiny and the DB is the
durable source of truth.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class DispatchStatus(str, Enum):
    ENQUEUED = "enqueued"
    DROPPED = "dropped"


@dataclass(slots=True)
class CandidateJob:
    request_id: str


JobHandler = Callable[[CandidateJob], Awaitable[None]]


@runtime_checkable
class CandidateDispatcher(Protocol):
    async def start(self, handler: JobHandler) -> None: ...

    async def enqueue(self, job: CandidateJob) -> DispatchStatus: ...

    async def stop(self, *, drain: bool = True) -> None: ...

    def depth(self) -> int: ...
