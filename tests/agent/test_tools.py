"""Spec §3.5: two action vocabularies, and they are not the same set. This is the one place
both are declared as data, checked against each other and against the seven that already
exist in `cua.surface.models.ActionKind`.
"""
from typing import get_args

from cua.agent.tools import (
    DISCOVERY_ONLY_TOOL_NAMES,
    DISCOVERY_TOOLS,
    REPLAY_TOOL_NAMES,
    validate_tool_call,
)
from cua.llm.base import ToolCall
from cua.surface.models import ActionKind


def test_replay_tool_names_match_the_closed_action_kind_vocabulary() -> None:
    # D28-style: one vocabulary, not a second copy that could drift from ActionKind.
    action_kinds = frozenset(get_args(ActionKind))
    assert action_kinds == REPLAY_TOOL_NAMES


def test_discovery_only_names_are_exactly_expand_finish_give_up() -> None:
    expected = frozenset({"expand", "finish", "give_up"})
    assert expected == DISCOVERY_ONLY_TOOL_NAMES


def test_the_two_vocabularies_are_disjoint_and_cover_every_tool() -> None:
    names = {t.name for t in DISCOVERY_TOOLS}
    assert names == REPLAY_TOOL_NAMES | DISCOVERY_ONLY_TOOL_NAMES
    overlap = REPLAY_TOOL_NAMES & DISCOVERY_ONLY_TOOL_NAMES
    assert not overlap


def test_validate_tool_call_accepts_a_well_formed_call() -> None:
    assert validate_tool_call(ToolCall(id="1", name="click", args={"index": 2})) is None


def test_validate_tool_call_rejects_an_unknown_tool() -> None:
    problem = validate_tool_call(ToolCall(id="1", name="delete_everything", args={}))
    assert problem is not None and "unknown tool" in problem


def test_validate_tool_call_rejects_a_missing_required_argument() -> None:
    problem = validate_tool_call(ToolCall(id="1", name="fill", args={"index": 0}))
    assert problem is not None and "value" in problem


def test_validate_tool_call_rejects_an_undeclared_argument() -> None:
    problem = validate_tool_call(
        ToolCall(id="1", name="click", args={"index": 0, "selector": "#x"})
    )
    assert problem is not None and "selector" in problem


def test_fill_and_select_and_press_key_all_require_a_value() -> None:
    for name in ("fill", "select", "press_key"):
        problem = validate_tool_call(ToolCall(id="1", name=name, args={"index": 0}))
        assert problem is not None, name


def test_finish_requires_a_checkpoint_index_and_a_summary() -> None:
    problem = validate_tool_call(ToolCall(id="1", name="finish", args={"summary": "done"}))
    assert problem is not None and "checkpoint_index" in problem
    assert validate_tool_call(
        ToolCall(id="1", name="finish", args={"summary": "done", "checkpoint_index": 3})
    ) is None
