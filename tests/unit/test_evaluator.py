from __future__ import annotations

import pytest

from shadow_proxy.evaluator import Verdict
from shadow_proxy.evaluator.json_action import JsonActionEvaluator


@pytest.fixture()
def evaluator() -> JsonActionEvaluator:
    return JsonActionEvaluator(compare_key="action", require_json=True)


def test_match_exact_action(evaluator: JsonActionEvaluator) -> None:
    r = evaluator.evaluate('{"action": "book_flight"}', '{"action": "book_flight"}')
    assert r.verdict is Verdict.MATCH
    assert r.primary_action == "book_flight"
    assert r.candidate_action == "book_flight"
    assert "action_match" in r.reasons


def test_match_case_and_whitespace_insensitive(evaluator: JsonActionEvaluator) -> None:
    r = evaluator.evaluate('{"action": " Book_Flight "}', '{"action": "book_flight"}')
    assert r.verdict is Verdict.MATCH


def test_mismatch(evaluator: JsonActionEvaluator) -> None:
    r = evaluator.evaluate('{"action": "book_flight"}', '{"action": "cancel_flight"}')
    assert r.verdict is Verdict.MISMATCH
    assert any(reason.startswith("action_mismatch") for reason in r.reasons)


def test_invalid_json_primary(evaluator: JsonActionEvaluator) -> None:
    r = evaluator.evaluate("not json", '{"action": "x"}')
    assert r.verdict is Verdict.INVALID_JSON
    assert any("primary_json_invalid" in reason for reason in r.reasons)


def test_invalid_json_candidate(evaluator: JsonActionEvaluator) -> None:
    r = evaluator.evaluate('{"action": "x"}', "not json")
    assert r.verdict is Verdict.INVALID_JSON


def test_missing_action_key(evaluator: JsonActionEvaluator) -> None:
    r = evaluator.evaluate('{"foo": 1}', '{"action": "x"}')
    assert r.verdict is Verdict.MISMATCH
    assert "missing_action_key" in r.reasons


def test_strip_code_fence(evaluator: JsonActionEvaluator) -> None:
    r = evaluator.evaluate(
        '```json\n{"action":"book_flight"}\n```',
        '{"action": "book_flight"}',
    )
    assert r.verdict is Verdict.MATCH


def test_empty_content(evaluator: JsonActionEvaluator) -> None:
    r = evaluator.evaluate("", "")
    assert r.verdict is Verdict.INVALID_JSON
