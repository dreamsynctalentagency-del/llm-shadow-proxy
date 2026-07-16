"""Evaluator protocol and shared verdicts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


class Verdict(str, Enum):
    PENDING = "pending"
    MATCH = "match"
    MISMATCH = "mismatch"
    INVALID_JSON = "invalid_json"
    CANDIDATE_ERROR = "candidate_error"
    PRIMARY_ERROR = "primary_error"


@dataclass(slots=True)
class EvaluationResult:
    verdict: Verdict
    reasons: list[str] = field(default_factory=list)
    primary_action: str | None = None
    candidate_action: str | None = None


@runtime_checkable
class Evaluator(Protocol):
    def evaluate(self, primary_text: str, candidate_text: str) -> EvaluationResult: ...
