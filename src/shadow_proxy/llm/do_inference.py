"""DigitalOcean Serverless Inference client.

DO's Serverless Inference exposes an OpenAI-compatible ``/v1/chat/completions``
endpoint at ``https://inference.do-ai.run``. We use ``httpx.AsyncClient``
directly (no OpenAI SDK dependency in the hot path) so we get full control over
connection pooling, timeouts, and error mapping.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from shadow_proxy.llm.base import ChatRequest, ChatResponse, LLMCallError, LLMTimeout

# Defense-in-depth: even if a caller bypassed Settings, we refuse to accept a
# key that looks like a placeholder. Kept in sync with settings._DUMMY_KEY_MARKERS.
_DUMMY_KEY_MARKERS: tuple[str, ...] = (
    "fake",
    "dummy",
    "example",
    "placeholder",
    "changeme",
    "your-key",
    "your_key",
    "yourkey",
    "todo",
    "xxx",
    "abc123",
    "paste-",
)
_MIN_KEY_LENGTH = 20


def _validate_api_key(api_key: str, *, strict: bool = True) -> None:
    if not api_key:
        raise ValueError(
            "DO_INFERENCE_API_KEY is required. "
            "Set it via env / .env, or route through Settings which explains how."
        )
    if not strict:
        return
    lowered = api_key.lower()
    for marker in _DUMMY_KEY_MARKERS:
        if marker in lowered:
            raise ValueError(
                f"DOInferenceClient refused a key that looks like a placeholder "
                f"(contains {marker!r})."
            )
    if len(api_key) < _MIN_KEY_LENGTH:
        raise ValueError(
            f"DOInferenceClient refused a key of length {len(api_key)} "
            f"(minimum {_MIN_KEY_LENGTH})."
        )


class DOInferenceClient:
    """Async client for DigitalOcean Serverless Inference.

    Set ``strict=False`` only for local development when the app is running
    with ``SHADOW_PROXY_ALLOW_DUMMY_KEY=1`` — real requests will still fail at
    DO (which will return 401), but the process can boot and the UI/schema
    endpoints work.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        max_retries: int = 0,
        connect_timeout_s: float = 5.0,
        limits: httpx.Limits | None = None,
        strict: bool = True,
    ) -> None:
        _validate_api_key(api_key, strict=strict)
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._max_retries = max(0, max_retries)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(connect=connect_timeout_s, read=None, write=None, pool=None),
            limits=limits or httpx.Limits(max_connections=200, max_keepalive_connections=50),
            http2=False,  # DO's endpoint currently prefers HTTP/1.1
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat_completions(
        self, req: ChatRequest, *, timeout_s: float
    ) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": req.model,
            "messages": [m.model_dump() for m in req.messages],
        }
        if req.temperature is not None:
            payload["temperature"] = req.temperature
        if req.max_completion_tokens is not None:
            payload["max_completion_tokens"] = req.max_completion_tokens
        for k, v in req.extra.items():
            payload.setdefault(k, v)

        attempts = self._max_retries + 1
        last_exc: Exception | None = None

        for attempt in range(attempts):
            try:
                resp = await self._client.post(
                    "/chat/completions",
                    json=payload,
                    timeout=timeout_s,
                )
            except httpx.TimeoutException as exc:
                raise LLMTimeout(f"DO inference timed out after {timeout_s}s") from exc
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < attempts - 1:
                    await asyncio.sleep(0.1 * (2**attempt))
                    continue
                raise LLMCallError(f"DO inference transport error: {exc}") from exc

            if resp.status_code >= 500 and attempt < attempts - 1:
                await asyncio.sleep(0.1 * (2**attempt))
                continue
            if resp.status_code >= 400:
                raise LLMCallError(
                    f"DO inference error {resp.status_code}: {resp.text[:500]}",
                    status_code=resp.status_code,
                )

            body = resp.json()
            return _to_chat_response(body)

        assert last_exc is not None
        raise LLMCallError(f"DO inference failed: {last_exc}") from last_exc


def _to_chat_response(body: dict[str, Any]) -> ChatResponse:
    try:
        choice = body["choices"][0]
        message = choice.get("message", {})
        content = message.get("content", "") or ""
        finish_reason = choice.get("finish_reason")
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMCallError(f"malformed DO inference response: {body!r}") from exc

    usage = body.get("usage") or {}
    return ChatResponse(
        id=body.get("id", ""),
        model=body.get("model", ""),
        content=content,
        finish_reason=finish_reason,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        total_tokens=usage.get("total_tokens"),
        raw=body,
    )
