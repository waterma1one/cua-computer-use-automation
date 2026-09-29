"""Spec §3.5's two action vocabularies, as data -- the replay-recordable eight and the
discovery-only three, plus the one place a raw `ToolCall` is checked against either.

Every parameter schema below is provider-neutral (`cua.llm.base.ToolDef`'s own contract,
E2) -- `cua.llm.gemini.GeminiClient` is the one place any of this is translated into a
specific provider's own schema vocabulary.
"""

from __future__ import annotations

from cua.llm.base import ToolCall, ToolDef

__all__ = [
    "DISCOVERY_ONLY_TOOL_NAMES",
    "DISCOVERY_TOOLS",
    "REPLAY_TOOL_NAMES",
    "validate_tool_call",
]


def _index_arg(description: str) -> dict[str, object]:
    return {"type": "integer", "description": description}


def _string_arg(description: str) -> dict[str, object]:
    return {"type": "string", "description": description}


def _obj(properties: dict[str, object], required: list[str]) -> dict[str, object]:
    return {"type": "object", "properties": properties, "required": required}


DISCOVERY_TOOLS: list[ToolDef] = [
    # -- Replay-recordable (spec §3.5's closed eight) --
    ToolDef("navigate", "Navigate to a path on the target application.",
            _obj({"path": _string_arg("The path to navigate to, e.g. /search.")}, ["path"])),
    ToolDef("click", "Click the control at this index in the current observation.",
            _obj({"index": _index_arg("The control's index.")}, ["index"])),
    ToolDef("fill", "Type a value into the textbox at this index.",
            _obj({"index": _index_arg("The textbox's index."),
                  "value": _string_arg("The text to type.")}, ["index", "value"])),
    ToolDef("select", "Choose an option in the control at this index.",
            _obj({"index": _index_arg("The control's index."),
                  "value": _string_arg("The option's visible text.")}, ["index", "value"])),
    ToolDef("press_key", "Press a key while the control at this index is focused.",
            _obj({"index": _index_arg("The control's index."),
                  "value": _string_arg("The key name, e.g. Enter.")}, ["index", "value"])),
    ToolDef("wait_for", "Wait for the control at this index to appear.",
            _obj({"index": _index_arg("The control's index.")}, ["index"])),
    ToolDef("read", "Read the text or value of the control at this index. Name output_name "
            "only if this reads a value the capability should return to its caller.",
            _obj({"index": _index_arg("The control's index."),
                  "output_name": _string_arg(
                      "The declared output this reads, if any. Omit for an internal "
                      "value only later steps need.")},
                 ["index"])),
    ToolDef("dismiss_dialog", "Dismiss the currently pending native dialog.", _obj({}, [])),
    # -- Discovery-only (spec §3.5's closed three; never recorded, never replayable) --
    ToolDef("expand", "Reveal more of the page than the current observation's budget shows.",
            _obj({}, [])),
    ToolDef("finish", "Declare the goal accomplished. checkpoint_index must name the index "
            "of the control in the current observation whose presence proves it.",
            _obj({"summary": _string_arg("What was accomplished."),
                  "checkpoint_index": _index_arg(
                      "The control that proves the goal was reached.")},
                 ["summary", "checkpoint_index"])),
    ToolDef("give_up", "Declare the goal cannot be accomplished from here.",
            _obj({"reason": _string_arg("Why the goal could not be reached.")}, ["reason"])),
]

REPLAY_TOOL_NAMES: frozenset[str] = frozenset({
    "navigate", "click", "fill", "select", "press_key", "wait_for", "read", "dismiss_dialog",
})
DISCOVERY_ONLY_TOOL_NAMES: frozenset[str] = frozenset({"expand", "finish", "give_up"})

_BY_NAME: dict[str, ToolDef] = {tool.name: tool for tool in DISCOVERY_TOOLS}


def validate_tool_call(call: ToolCall) -> str | None:
    """`None` if `call` names a known tool and its arguments match that tool's declared
    `required`/`properties` exactly; an explanatory message otherwise. The discovery loop
    (Task 5) counts a non-`None` result as one malformed turn toward the consecutive-failure
    stopping condition.
    """
    tool = _BY_NAME.get(call.name)
    if tool is None:
        return f"unknown tool {call.name!r}"
    properties = tool.parameters.get("properties", {})
    required = tool.parameters.get("required", [])
    assert isinstance(properties, dict) and isinstance(required, list)
    missing = [name for name in required if name not in call.args]
    if missing:
        return f"{call.name!r} is missing required argument(s): {', '.join(missing)}"
    extra = [name for name in call.args if name not in properties]
    if extra:
        return f"{call.name!r} does not accept argument(s): {', '.join(extra)}"
    return None
