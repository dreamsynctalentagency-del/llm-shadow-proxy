"""Deterministic hashing of request bodies (used as an idempotency key)."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _canonicalize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _canonicalize(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [_canonicalize(v) for v in obj]
    return obj


def request_hash(body: dict[str, Any]) -> str:
    """SHA-256 over a canonicalized (sorted keys, stable order) JSON encoding."""
    canonical = _canonicalize(body)
    encoded = json.dumps(canonical, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
