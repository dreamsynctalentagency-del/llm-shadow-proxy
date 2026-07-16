"""Deterministic JSON-based comparator.

Rules (both must hold to reach ``match``):

1. Both responses parse as JSON.
2. The configured key (default ``action``) exists in both.
3. After normalization (lowercase + strip whitespace by default) the values match.
"""

from __future__ import annotations

import json
import re
from typing import Any

from shadow_proxy.evaluator.base import EvaluationResult, Verdict

_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$",
    re.DOTALL | re.IGNORECASE,
)


def _strip_code_fence(text: str) -> str:
    m = _FENCE_RE.match(text)
    if m:
        return m.group("body")
    return text.strip()


class JsonActionEvaluator:
    """Compare two LLM outputs by extracting and matching a JSON key."""

    def __init__(
        self,
        *,
        compare_key: str = "action",
        require_json: bool = True,
        normalize: str = "lowercase_strip",
    ) -> None:
        self._compare_key = compare_key
        self._require_json = require_json
        self._normalize = normalize

    def _normalize_value(self, value: Any) -> str:
        s = str(value)
        if self._normalize == "lowercase_strip":
            return s.strip().lower()
        return s

    def _parse(self, text: str) -> tuple[Any, str | None]:
        """Return (parsed_json, error_reason)."""
        stripped = _strip_code_fence(text or "")
        if not stripped:
            return None, "empty_content"
        try:
            return json.loads(stripped), None
        except json.JSONDecodeError as exc:
            return None, f"json_decode_error:{exc.msg}"

    def evaluate(self, primary_text: str, candidate_text: str) -> EvaluationResult:
        reasons: list[str] = []

        p_obj, p_err = self._parse(primary_text)
        if p_err is None:
            reasons.append("primary_json_ok")
        else:
            reasons.append(f"primary_json_invalid:{p_err}")

        c_obj, c_err = self._parse(candidate_text)
        if c_err is None:
            reasons.append("candidate_json_ok")
        else:
            reasons.append(f"candidate_json_invalid:{c_err}")

        if self._require_json and (p_err is not None or c_err is not None):
            return EvaluationResult(verdict=Verdict.INVALID_JSON, reasons=reasons)

        p_action = _get_key(p_obj, self._compare_key)
        c_action = _get_key(c_obj, self._compare_key)

        p_action_norm = self._normalize_value(p_action) if p_action is not None else None
        c_action_norm = self._normalize_value(c_action) if c_action is not None else None

        if p_action is None or c_action is None:
            reasons.append("missing_action_key")
            return EvaluationResult(
                verdict=Verdict.MISMATCH,
                reasons=reasons,
                primary_action=None if p_action is None else str(p_action),
                candidate_action=None if c_action is None else str(c_action),
            )

        if p_action_norm == c_action_norm:
            reasons.append("action_match")
            return EvaluationResult(
                verdict=Verdict.MATCH,
                reasons=reasons,
                primary_action=str(p_action),
                candidate_action=str(c_action),
            )

        reasons.append(f"action_mismatch:{p_action!r}!={c_action!r}")
        return EvaluationResult(
            verdict=Verdict.MISMATCH,
            reasons=reasons,
            primary_action=str(p_action),
            candidate_action=str(c_action),
        )


def _get_key(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return None
