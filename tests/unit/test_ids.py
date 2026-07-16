from __future__ import annotations

from shadow_proxy.util.ids import is_valid_request_id, new_request_id


def test_new_request_id_valid() -> None:
    rid = new_request_id()
    assert is_valid_request_id(rid)
    assert len(rid) == 26


def test_invalid_request_id() -> None:
    assert not is_valid_request_id("not-a-ulid")
