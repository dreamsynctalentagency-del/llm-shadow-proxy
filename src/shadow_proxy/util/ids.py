"""Correlation ID helpers."""

from __future__ import annotations

from ulid import ULID


def new_request_id() -> str:
    """Return a lexicographically sortable ULID string."""
    return str(ULID())


def is_valid_request_id(value: str) -> bool:
    try:
        ULID.from_str(value)
    except (ValueError, TypeError):
        return False
    return True
