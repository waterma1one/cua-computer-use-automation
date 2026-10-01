"""Bounded retry with exponential backoff around any `LLMClient`.

Applied at the CLI boundary (`cua discover`), not inside the discovery loop: the loop treats
an `LLMError` as one failed turn, and this wrapper makes a transient provider failure
(rate limit, 5xx, network blip) cost one retried request rather than one failed turn.
Only `LLMError` is retried; any other exception is a bug and propagates at once.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from cua.llm.base import Completion, LLMClient, Message, ToolCall, ToolDef
from cua.llm.gemini import LLMError

__all__ = ["RetryingClient"]


class RetryingClient:
    """`LLMClient` (structurally). `sleep` is injectable so tests never wait."""

    def __init__(
        self, inner: LLMClient, *, attempts: int = 3, base_delay_s: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._inner = inner
        self._attempts = max(1, attempts)
        self._base_delay_s = base_delay_s
        self._sleep = sleep

    @property
    def model(self) -> str:
        return str(getattr(self._inner, "model", "unknown"))

    def step(self, messages: list[Message], tools: list[ToolDef]) -> ToolCall | Completion:
        last: LLMError | None = None
        for attempt in range(self._attempts):
            try:
                return self._inner.step(messages, tools)
            except LLMError as exc:
                last = exc
                if attempt + 1 < self._attempts:
                    self._sleep(self._base_delay_s * (2 ** attempt))
        assert last is not None
        raise last
