"""Very lightweight PII redactor used before logging.

Deliberately conservative — masks anything that looks like an email or an
Authorization header value. Not a substitute for a real DLP pipeline, but
prevents obvious secret leakage into logs.
"""

from __future__ import annotations

import re
from typing import Any

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_BEARER_RE = re.compile(r"(?i)bearer\s+[a-z0-9\-_\.]+")


def _redact_str(s: str) -> str:
    s = _EMAIL_RE.sub("[REDACTED_EMAIL]", s)
    return _BEARER_RE.sub("Bearer [REDACTED]", s)


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_str(value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value
