"""A scripted `LLMClient` double. Every test in this phase that does not test the real
Gemini adapter itself uses this -- no test in `cua/agent/` needs a key or a network.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cua.llm.base import Completion, LLMClient, Message, ToolCall, ToolDef

__all__ = ["FakeClient"]


@dataclass
class FakeClient:
    """Returns `script[0]`, then `script[1]`, ... in order, one per `step()` call. Records
    every call's `(messages, tools)` pair in `self.calls` so a test can assert on exactly
    what the loop sent -- the observation message it built, the tool list it offered.
    """

    script: list[ToolCall | Completion]
    calls: list[tuple[list[Message], list[ToolDef]]] = field(default_factory=list)
    _next: int = field(default=0, repr=False)

    def step(self, messages: list[Message], tools: list[ToolDef]) -> ToolCall | Completion:
        self.calls.append((messages, tools))
        if self._next >= len(self.script):
            raise AssertionError("FakeClient script exhausted")
        result = self.script[self._next]
        self._next += 1
        return result


assert isinstance(FakeClient(script=[]), LLMClient)  # module import time: conformance
