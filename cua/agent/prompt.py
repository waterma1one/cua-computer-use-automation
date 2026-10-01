"""Turns a goal and an Observation into the messages the model sees each turn. Deliberately
small: the model's whole context each turn is the goal (stated once, as the system message)
plus whatever tool traffic followed its own prior calls (`history`, which `cua.agent.loop`
maintains) plus the current observation, rendered as a compact, indexed list. Earlier
observations are not repeated -- the indices in them are stale the moment the page moves on.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from cua.llm.base import Message
from cua.surface.models import Observation

__all__ = [
    "INPUTS_PROMPT",
    "SYSTEM_PROMPT",
    "PromptInput",
    "build_messages",
    "observation_message",
    "system_message",
]

SYSTEM_PROMPT = (
    "You are exploring a web application to accomplish a goal, one action at a time. Every "
    "turn you will be shown the currently visible controls, each with an index. Call exactly "
    "one tool per turn, naming a control by its index. When the goal is accomplished, call "
    "finish and name the index of the control that proves it. If the goal cannot be reached "
    "from here, call give_up and say why.\n\nGoal: {goal}"
)


INPUTS_PROMPT = (
    "\n\nThis task has declared inputs. To use one, type its placeholder, exactly "
    "`{{{{name}}}}` and nothing else, as the `value` of fill, select or press_key; the "
    "real value is substituted for you. Never invent, guess or ask for a value for these "
    "fields. All other literal text you type is recorded as a literal.\nInputs:\n{lines}"
)


@dataclass(frozen=True)
class PromptInput:
    """What the model may know about one declared input. `example` is shown only when the
    input is not sensitive; a sensitive input's value never reaches this module's output."""

    name: str
    sensitive: bool
    example: str


def _input_line(item: PromptInput) -> str:
    if item.sensitive:
        return f"- {{{{{item.name}}}}} (sensitive; value withheld)"
    return f"- {{{{{item.name}}}}} (example value: {item.example!r})"


def system_message(goal: str, inputs: Sequence[PromptInput] | None = None) -> Message:
    text = SYSTEM_PROMPT.format(goal=goal)
    if inputs:
        text += INPUTS_PROMPT.format(lines="\n".join(_input_line(i) for i in inputs))
    return Message(role="system", text=text)


def observation_message(observation: Observation) -> Message:
    lines = [f"{n.index}: {n.role}" + (f' "{n.name}"' if n.name else "")
             + (f" = {n.value!r}" if n.value else "")
             + (" [disabled]" if n.state.disabled else "")
             for n in observation.nodes]
    truncation = " (truncated -- call expand to see more)" if observation.truncated else ""
    return Message(role="user", text="Visible controls:" + truncation + "\n" + "\n".join(lines))


def build_messages(
    goal: str, observation: Observation, history: list[Message],
    inputs: Sequence[PromptInput] | None = None,
) -> list[Message]:
    """The full message list for one turn: system prompt, prior tool traffic, then the
    current observation. Returns a new list; `history` is never mutated."""
    return [system_message(goal, inputs), *history, observation_message(observation)]
