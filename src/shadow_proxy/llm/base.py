"""LLMClient protocol and shared data models.

The provider-specific client (currently DigitalOcean Serverless Inference) is
just one implementation; anything that fulfils :class:`LLMClient` can be plugged
in via configuration. This is the seam that keeps the app portable across
providers.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class ChatRequest(BaseModel):
    """Provider-agnostic chat completion request."""

    model: str
    messages: list[ChatMessage]
    temperature: float | None = None
    max_completion_tokens: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    """Provider-agnostic chat completion response."""

    id: str
    model: str
    content: str  # convenience: choices[0].message.content
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)  # full provider payload


class LLMCallError(RuntimeError):
    """Recoverable/known error from an LLM upstream (HTTP 4xx/5xx, malformed)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMTimeout(LLMCallError):
    """The LLM call did not complete within the configured timeout."""


@runtime_checkable
class LLMClient(Protocol):
    """Any async chat completion client."""

    async def chat_completions(self, req: ChatRequest, *, timeout_s: float) -> ChatResponse: ...

    async def aclose(self) -> None: ...
