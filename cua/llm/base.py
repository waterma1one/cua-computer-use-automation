"""The provider-agnostic LLM abstraction (spec §8.2): the discovery loop's only requirement
of a model is structured tool calls. Every adapter (`cua.llm.gemini.GeminiClient`,
`cua.llm.fake.FakeClient`) satisfies `LLMClient` structurally -- this module imports neither.

`ToolDef` lives here, not in `cua/agent/tools.py` (E2): `cua/llm/` must not import
`cua/agent/` (the dependency runs the other way, the same one-way discipline `EvidenceSink`/
`Escalator` already established), and `LLMClient.step`'s signature needs a type for `tools`
regardless of which package eventually declares the concrete discovery tool list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

__all__ = ["Completion", "LLMClient", "Message", "ToolCall", "ToolDef"]


@dataclass(frozen=True)
class Message:
    """One turn in the conversation the loop builds up. `tool_name`/`tool_call_id` are set
    only on a `role="tool"` message -- the result of executing a prior `ToolCall`, fed back
    so the model can see what happened.
    """

    role: Literal["system", "user", "model", "tool"]
    text: str = ""
    tool_name: str | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True)
class ToolCall:
    """The model asked to execute one of the tools it was offered."""

    id: str
    name: str
    args: dict[str, object]


@dataclass(frozen=True)
class Completion:
    """The model returned free text instead of a tool call. Never a valid discovery turn on
    its own (the loop only ever offers tools and expects one back) -- counted as a malformed
    turn by the stopping-condition machinery (Task 5).
    """

    text: str


@dataclass(frozen=True)
class ToolDef:
    """One tool the model may call, as a provider-neutral JSON-Schema function declaration.
    `parameters` follows the same lower-case-`type` convention
    `cua.artifact.models.InputSpec`/`OutputSpec` already use -- an adapter (`GeminiClient`)
    translates it into its own provider's schema vocabulary at the boundary, not before.
    """

    name: str
    description: str
    parameters: dict[str, object]


@runtime_checkable
class LLMClient(Protocol):
    """§8.2's whole interface, verbatim."""

    def step(self, messages: list[Message], tools: list[ToolDef]) -> ToolCall | Completion:
        """Given the conversation so far and the tools on offer, returns exactly one tool
        call or one free-text completion. Never raises for an ordinary model response;
        an adapter's own transport/auth failures are its problem to raise clearly (see
        `cua.llm.gemini.LLMError`).
        """
        ...
