from __future__ import annotations

from shadow_proxy.util.hashing import request_hash


def test_deterministic_hash_ignores_key_order() -> None:
    a = {"messages": [{"role": "user", "content": "hi"}], "temperature": 0.0}
    b = {"temperature": 0.0, "messages": [{"role": "user", "content": "hi"}]}
    assert request_hash(a) == request_hash(b)


def test_hash_differs_on_content_change() -> None:
    a = {"messages": [{"role": "user", "content": "hi"}]}
    b = {"messages": [{"role": "user", "content": "hello"}]}
    assert request_hash(a) != request_hash(b)
