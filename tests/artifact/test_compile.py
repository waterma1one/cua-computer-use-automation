"""Spec §8.3 steps 1-6, offline -- no browser, no surface, just a Trace already captured by
Task 5's loop. The scenario throughout is the same member-search-then-read-balance flow every
other phase's fixtures use.
"""
import dataclasses

import pytest

from cua.agent.loop import DeclaredInput, Trace, TraceStep
from cua.artifact.compile import CompileError
from cua.artifact.compile import compile as compile_artifact
from cua.artifact.models import (
    App,
    FromInput,
    FromStep,
    InputSpec,
    LiteralValue,
    OutputSpec,
    Provenance,
    Settle,
)
from cua.llm.base import ToolCall
from cua.surface.locators import synthesize
from cua.surface.models import Observation
from tests.agent.conftest import node


def _target() -> App:
    return App(vendor_product="corebank-teller", variant="base", surface="web",
              entry="/teller/index.html")


def _provenance() -> Provenance:
    return Provenance(discovered_at="2026-09-24T00:00:00", model="gemini-3.5-flash-lite",
                      policy_mode="sandbox", provider_retention="training_permitted",
                      run_id="run-20260924000000-abcd",
                      trace_ref="evidence/run-20260924000000-abcd/trace.jsonl")


def _fill_step(index: int, value: str, node_name: str) -> TraceStep:
    n = node("textbox", node_name, index=index)
    obs = Observation(generation=1, nodes=[n], truncated=False)
    return TraceStep(index=1, tool_call=ToolCall(id="1", name="fill",
                     args={"index": index, "value": value}),
                     discovery_only=False, human_origin=False, ok=True, read_value=None,
                     locator=synthesize(n, [n]), observation=obs, raw_nodes=[n])


def _read_step(index: int, node_name: str, value: str, output_name: str | None) -> TraceStep:
    n = node("cell", node_name, value=value, index=index)
    obs = Observation(generation=2, nodes=[n], truncated=False)
    args: dict[str, object] = {"index": index}
    if output_name is not None:
        args["output_name"] = output_name
    return TraceStep(index=2, tool_call=ToolCall(id="2", name="read", args=args),
                     discovery_only=False, human_origin=False, ok=True, read_value=value,
                     locator=synthesize(n, [n]), observation=obs, raw_nodes=[n])


def _trace(steps, stop_reason="finish", declared_inputs=None, checkpoint_index=0) -> Trace:
    # Every fixture step above (_fill_step/_read_step) observes a single node at index 0,
    # so 0 is always a valid checkpoint for a test that does not care about the checkpoint
    # itself -- tests that do (test_compile_sets_the_success_checkpoint_...) build their own
    # Trace directly instead of going through this helper. final_observation is set to the
    # fixed, value-free node here (a read cell would rightly be refused as a checkpoint);
    # it is not what discover() itself would
    # produce -- see the Trace docstring.
    return Trace(goal="find member and read balance", target=_target(),
                declared_inputs=declared_inputs or {}, steps=steps, stop_reason=stop_reason,
                stop_detail="done",
                checkpoint_index=checkpoint_index if stop_reason == "finish" else None,
                final_observation=Observation(
                    generation=9, nodes=[node("statictext", "Done", index=0)], truncated=False,
                ) if steps and stop_reason == "finish" else None)


def test_compile_refuses_a_trace_that_did_not_finish() -> None:
    with pytest.raises(CompileError, match="did not finish"):
        compile_artifact(
            _trace([], stop_reason="give_up"), id="x", version=1, name="x", description="x",
            app=_target(), settle=Settle(timeout_ms=1000, poll_ms=50), max_duration_ms=60000,
            outputs={}, provenance=_provenance(),
        )


def test_compile_promotes_a_literal_matching_a_declared_input() -> None:
    steps = [_fill_step(0, "12345", "Member ID"), _read_step(0, "Savings", "1234.56", "balance")]
    trace = _trace(steps, declared_inputs={
        "member_id": DeclaredInput(spec=InputSpec(type="string", pattern="^[0-9]{5}$"),
                                  example_value="12345"),
    })
    artifact = compile_artifact(
        trace, id="corebank.probe", version=1, name="probe", description="d", app=_target(),
        settle=Settle(timeout_ms=1000, poll_ms=50), max_duration_ms=60000,
        outputs={"balance": OutputSpec(type="string", format="money")}, provenance=_provenance(),
    )
    assert artifact.steps[1].value == FromInput(from_input="member_id")
    assert artifact.verified is False


def test_compile_refuses_a_literal_from_a_protected_field_with_no_declared_input() -> None:
    steps = [_fill_step(0, "hunter2", "PIN"), _read_step(0, "Savings", "1234.56", "balance")]
    with pytest.raises(CompileError, match="protected"):
        compile_artifact(
            _trace(steps), id="x", version=1, name="x", description="x", app=_target(),
            settle=Settle(timeout_ms=1000, poll_ms=50), max_duration_ms=60000, outputs={},
            provenance=_provenance(),
        )


def test_compile_keeps_an_unmatched_unprotected_literal_as_a_literal() -> None:
    steps = [_fill_step(0, "some text", "Notes"), _read_step(0, "Savings", "1234.56", "balance")]
    artifact = compile_artifact(
        _trace(steps), id="x", version=1, name="x", description="x", app=_target(),
        settle=Settle(timeout_ms=1000, poll_ms=50), max_duration_ms=60000, outputs={},
        provenance=_provenance(),
    )
    assert artifact.steps[1].value == LiteralValue(literal="some text")


def test_compile_binds_a_named_output_and_a_local_value_differently() -> None:
    steps = [
        _fill_step(0, "12345", "Member ID"),
        _read_step(0, "Savings", "1234.56", "balance"),
        _read_step(0, "Account Type", "Savings", None),
    ]
    artifact = compile_artifact(
        _trace(steps, declared_inputs={"member_id": DeclaredInput(
            spec=InputSpec(type="string"), example_value="12345")}),
        id="x", version=1, name="x", description="x", app=_target(),
        settle=Settle(timeout_ms=1000, poll_ms=50), max_duration_ms=60000,
        outputs={"balance": OutputSpec(type="string")}, provenance=_provenance(),
    )
    assert artifact.steps[2].into == "balance"
    assert artifact.steps[3].into == "_s4"


def _compile_with_final(steps, final_nodes, checkpoint_index):
    trace = Trace(goal="g", target=_target(), declared_inputs={
        "member_id": DeclaredInput(spec=InputSpec(type="string"), example_value="12345")},
        steps=steps, stop_reason="finish", stop_detail="done",
        checkpoint_index=checkpoint_index,
        final_observation=Observation(generation=3, nodes=final_nodes, truncated=False))
    return compile_artifact(
        trace, id="x", version=1, name="x", description="x", app=_target(),
        settle=Settle(timeout_ms=1000, poll_ms=50), max_duration_ms=60000,
        outputs={"balance": OutputSpec(type="string")}, provenance=_provenance(),
    )


def test_compile_derives_a_next_step_lookahead_expect_and_leaves_the_last_step_empty() -> None:
    steps = [_fill_step(0, "12345", "Member ID"), _fill_step(0, "x", "Memo")]
    artifact = compile_artifact(
        _trace(steps, declared_inputs={"member_id": DeclaredInput(
            spec=InputSpec(type="string"), example_value="12345")}),
        id="x", version=1, name="x", description="x", app=_target(),
        settle=Settle(timeout_ms=1000, poll_ms=50), max_duration_ms=60000,
        outputs={}, provenance=_provenance(),
    )
    assert len(artifact.steps[1].expects) == 1
    assert artifact.steps[1].expects[0].source == "observed"
    assert artifact.steps[1].expects[0].verified is True
    assert artifact.steps[2].expects == []


def test_compile_emits_no_expect_before_a_read_step() -> None:
    # The read target's name is the discovered value; pinning it would fail any other input.
    steps = [_fill_step(0, "12345", "Member ID"), _read_step(0, "4,218.60", "4,218.60", "balance")]
    artifact = compile_artifact(
        _trace(steps, declared_inputs={"member_id": DeclaredInput(
            spec=InputSpec(type="string"), example_value="12345")}),
        id="x", version=1, name="x", description="x", app=_target(),
        settle=Settle(timeout_ms=1000, poll_ms=50), max_duration_ms=60000,
        outputs={"balance": OutputSpec(type="string")}, provenance=_provenance(),
    )
    assert artifact.steps[1].expects == []


def test_compile_never_pins_a_read_value_in_the_checkpoint() -> None:
    read = _read_step(0, "4,218.60", "4,218.60", "balance")
    heading = node("heading", "Account summary", index=0)
    cell = node("cell", "4,218.60", index=1)
    artifact = _compile_with_final([_fill_step(0, "12345", "Member ID"), read],
                                   [heading, cell], 1)
    checkpoint = artifact.success.checkpoint
    assert checkpoint.name == "Account summary"
    assert checkpoint.role == "heading"
    assert "4,218.60" not in checkpoint.model_dump_json()


def test_compile_refuses_a_value_only_checkpoint_with_no_stable_neighbour() -> None:
    read = _read_step(0, "4,218.60", "4,218.60", "balance")
    cell = node("cell", "Balance 4,218.60", index=0)
    with pytest.raises(CompileError, match="read value"):
        _compile_with_final([_fill_step(0, "12345", "Member ID"), read], [cell], 0)


def test_compile_keeps_a_checkpoint_that_holds_no_read_value() -> None:
    read = _read_step(0, "4,218.60", "4,218.60", "balance")
    done = node("statictext", "Balance updated", index=0)
    artifact = _compile_with_final([_fill_step(0, "12345", "Member ID"), read], [done], 0)
    assert artifact.success.checkpoint.name == "Balance updated"


def test_compile_sets_the_success_checkpoint_from_finishs_checkpoint_index() -> None:
    fill = _fill_step(0, "12345", "Member ID")
    read = _read_step(0, "Savings", "1234.56", "balance")
    read = TraceStep(**{**read.__dict__, "tool_call": ToolCall(
        id="2", name="read", args={"index": 0, "output_name": "balance"})})
    # The observation the model held when it called `finish` is captured by discover()'s
    # loop *after* the last recorded action's own post-action `surface.observe()` -- a
    # distinct object from `read.observation` whenever, as here, at least one more
    # observation happens before finishing. Trace.final_observation carries that; using
    # read.observation instead (the old, wrong reading) would resolve checkpoint_index=0
    # to "Savings", not "Balance updated" -- the regression this test exists to catch.
    final_node = node("statictext", "Balance updated", index=0)
    final_observation = Observation(generation=3, nodes=[final_node], truncated=False)
    trace = Trace(goal="g", target=_target(), declared_inputs={
        "member_id": DeclaredInput(spec=InputSpec(type="string"), example_value="12345")},
        steps=[fill, read], stop_reason="finish", stop_detail="read the balance",
        checkpoint_index=0, final_observation=final_observation)
    artifact = compile_artifact(
        trace, id="x", version=1, name="x", description="x", app=_target(),
        settle=Settle(timeout_ms=1000, poll_ms=50), max_duration_ms=60000,
        outputs={"balance": OutputSpec(type="string")}, provenance=_provenance(),
    )
    assert artifact.success.checkpoint.name == "Balance updated"


def test_compile_drops_discovery_only_and_human_origin_steps() -> None:
    expand_step = _fill_step(0, "12345", "Member ID")
    object.__setattr__(expand_step, "discovery_only", True)
    human_step = _read_step(0, "Savings", "1234.56", "balance")
    object.__setattr__(human_step, "human_origin", True)
    real_step = _read_step(0, "Checking", "78.90", "balance")
    artifact = compile_artifact(
        _trace([expand_step, human_step, real_step]), id="x", version=1, name="x",
        description="x", app=_target(), settle=Settle(timeout_ms=1000, poll_ms=50),
        max_duration_ms=60000, outputs={"balance": OutputSpec(type="string")},
        provenance=_provenance(),
    )
    assert len(artifact.steps) == 2
    # The kept read's cell holds its read value, so it is located by role and position.
    assert artifact.steps[1].locator is not None
    assert artifact.steps[1].locator.name_match == "any"


def test_compile_runs_load_time_validation_and_refuses_an_invalid_artifact() -> None:
    # An id containing '://' trips criterion 1's forbidden-content scan (cua.artifact.validate).
    steps = [_read_step(0, "Savings", "1234.56", "balance")]
    with pytest.raises(CompileError):
        compile_artifact(
            _trace(steps), id="http://not-an-id", version=1, name="x", description="x",
            app=_target(), settle=Settle(timeout_ms=1000, poll_ms=50), max_duration_ms=60000,
            outputs={"balance": OutputSpec(type="string")}, provenance=_provenance(),
        )


def test_compile_normalises_a_model_proposed_output_name() -> None:
    steps = [_read_step(0, "Savings", "1234.56", "Account Balance!")]
    artifact = compile_artifact(
        _trace(steps), id="x", version=1, name="x", description="x", app=_target(),
        settle=Settle(timeout_ms=1000, poll_ms=50), max_duration_ms=60000,
        outputs={"account_balance": OutputSpec(type="string")}, provenance=_provenance(),
    )
    assert artifact.steps[1].into == "account_balance"


def test_compile_refuses_a_finished_trace_with_no_final_observation() -> None:
    steps = [_read_step(0, "Savings", "1234.56", "balance")]
    trace = Trace(goal="g", target=_target(), declared_inputs={}, steps=steps,
                  stop_reason="finish", stop_detail="d", checkpoint_index=0,
                  final_observation=None)
    with pytest.raises(CompileError, match="final observation"):
        compile_artifact(
            trace, id="x", version=1, name="x", description="x", app=_target(),
            settle=Settle(timeout_ms=1000, poll_ms=50), max_duration_ms=60000,
            outputs={"balance": OutputSpec(type="string")}, provenance=_provenance(),
        )


def test_compile_refuses_an_out_of_range_checkpoint_index() -> None:
    steps = [_read_step(0, "Savings", "1234.56", "balance")]
    trace = _trace(steps, checkpoint_index=5)
    with pytest.raises(CompileError, match="out of range"):
        compile_artifact(
            trace, id="x", version=1, name="x", description="x", app=_target(),
            settle=Settle(timeout_ms=1000, poll_ms=50), max_duration_ms=60000,
            outputs={"balance": OutputSpec(type="string")}, provenance=_provenance(),
        )


def test_compile_promotes_a_value_matching_an_earlier_local_read_to_from_step() -> None:
    steps = [
        _read_step(0, "Account Type", "Savings", None),
        _fill_step(0, "Savings", "Notes"),
    ]
    artifact = compile_artifact(
        _trace(steps), id="x", version=1, name="x", description="x", app=_target(),
        settle=Settle(timeout_ms=1000, poll_ms=50), max_duration_ms=60000, outputs={},
        provenance=_provenance(),
    )
    assert artifact.steps[1].id == "s2"
    assert artifact.steps[1].into == "_s2"
    assert artifact.steps[2].value == FromStep(from_step="s2")


def _nav_step(path: str) -> TraceStep:
    n = node("link", "Home", index=0)
    obs = Observation(generation=1, nodes=[n], truncated=False)
    return TraceStep(index=1, tool_call=ToolCall(id="n", name="navigate", args={"path": path}),
                     discovery_only=False, human_origin=False, ok=True, read_value=None,
                     locator=None, observation=obs, raw_nodes=[n])


def _compile_notes(steps) -> list:
    return compile_artifact(
        _trace(steps), id="x", version=1, name="x", description="x", app=_target(),
        settle=Settle(timeout_ms=1000, poll_ms=50), max_duration_ms=60000, outputs={},
        provenance=_provenance(),
    ).steps


def test_compile_prepends_a_navigate_to_the_entry_path() -> None:
    steps = _compile_notes([_fill_step(0, "some text", "Notes")])
    assert steps[0].action == "navigate"
    assert steps[0].target is not None and steps[0].target.path == "/teller/index.html"
    assert steps[0].id == "s1"
    assert steps[1].action == "fill" and steps[1].id == "s2"


def test_compile_does_not_duplicate_a_leading_navigate_to_the_entry_path() -> None:
    steps = _compile_notes(
        [_nav_step("/teller/index.html"), _fill_step(0, "some text", "Notes")]
    )
    assert [s.action for s in steps] == ["navigate", "fill"]


def test_compile_prepends_when_the_leading_navigate_goes_elsewhere() -> None:
    steps = _compile_notes([_nav_step("/other"), _fill_step(0, "some text", "Notes")])
    assert [s.action for s in steps] == ["navigate", "navigate", "fill"]


def _table_read_step(value: str) -> TraceStep:
    from cua.surface.models import NodeState

    rows = [("Checking", "4,218.60"), ("Savings", value)]
    nodes = []
    for label, amount in rows:
        for text in (label, amount):
            nodes.append(node("cell", text, index=len(nodes)).model_copy(
                update={"state": NodeState()}))
    target = nodes[3]
    obs = Observation(generation=2, nodes=nodes, truncated=False)
    return TraceStep(index=2, tool_call=ToolCall(id="2", name="read",
                     args={"index": 3, "output_name": "balance"}),
                     discovery_only=False, human_origin=False, ok=True, read_value=value,
                     locator=synthesize(target, nodes), observation=obs, raw_nodes=nodes)


def test_compile_gives_a_read_of_a_value_cell_a_value_independent_locator() -> None:
    read = _table_read_step("89,410.22")
    artifact = _compile_with_final([_fill_step(0, "12345", "Member ID"), read],
                                   [node("statictext", "Done", index=0)], 0)
    loc = artifact.steps[-1].locator
    assert loc is not None
    assert loc.name is None and loc.name_match == "any" and loc.role == "cell"
    assert loc.ordinal == 3 and loc.confidence == "low"
    assert "89,410.22" not in artifact.model_dump_json()


def test_compile_keeps_the_synthesized_locator_for_a_read_of_a_label() -> None:
    read = _table_read_step("89,410.22")
    label = read.raw_nodes[2]
    read2 = dataclasses.replace(
        read, tool_call=ToolCall(id="2", name="read", args={"index": 2, "output_name": "balance"}),
        locator=synthesize(label, read.raw_nodes),
    )
    artifact = _compile_with_final([_fill_step(0, "12345", "Member ID"), read2],
                                   [node("statictext", "Done", index=0)], 0)
    loc = artifact.steps[-1].locator
    assert loc is not None and loc.name == "Savings" and loc.name_match == "exact"


def test_artifact_with_a_value_independent_locator_round_trips() -> None:
    read = _table_read_step("89,410.22")
    artifact = _compile_with_final([_fill_step(0, "12345", "Member ID"), read],
                                   [node("statictext", "Done", index=0)], 0)
    from cua.artifact.validate import validate

    again = type(artifact).model_validate_json(artifact.model_dump_json())
    assert again == artifact
    assert not [f for f in validate(again) if f.level == "error"]


def test_a_matcher_rejects_name_match_any() -> None:
    from pydantic import ValidationError

    from cua.artifact.models import Matcher

    with pytest.raises(ValidationError):
        Matcher(strategy="role_name", role="cell", name=None, name_match="any")


def _member_page(extra=()):
    return [
        node("text", "Member 22222 Priya Raghunathan SSN ***-**-7742 Acct ********7431", index=0),
        node("cell", "Type", index=1), node("cell", "Number", index=2),
        node("cell", "Balance", index=3), node("cell", "Savings", index=4),
        node("cell", "********7431-01", index=5), node("cell", "89,410.22", index=6),
        node("button", "Select", index=7), node("link", "Print statement", index=8),
        node("link", "Open sub-account", index=9), *extra,
    ]


def _read_balance():
    return _read_step(0, "89,410.22", "89,410.22", "balance")


def test_compile_falls_back_to_a_digit_free_cell_when_there_is_no_label_role() -> None:
    artifact = _compile_with_final([_fill_step(0, "12345", "Member ID"), _read_balance()],
                                   _member_page(), 6)
    checkpoint = artifact.success.checkpoint
    assert checkpoint.role == "cell"
    assert checkpoint.name in {"Type", "Number", "Balance", "Savings"}
    assert not any(ch.isdigit() for ch in checkpoint.name or "")
    assert "89,410.22" not in checkpoint.model_dump_json()
    assert "12345" not in checkpoint.model_dump_json()
    assert checkpoint.name == "Savings"  # nearest preceding digit-free node; index 5 has digits


def test_compile_fallback_skips_a_node_holding_a_declared_input_value() -> None:
    nodes = [node("cell", "Type", index=0), node("cell", "Member Alpha", index=1),
             node("cell", "89,410.22", index=2)]
    trace_inputs = {"member_id": DeclaredInput(spec=InputSpec(type="string"),
                                               example_value="Alpha")}
    trace = Trace(goal="g", target=_target(), declared_inputs=trace_inputs,
                  steps=[_fill_step(0, "Alpha", "Member ID"), _read_balance()],
                  stop_reason="finish", stop_detail="done", checkpoint_index=2,
                  final_observation=Observation(generation=3, nodes=nodes, truncated=False))
    artifact = compile_artifact(
        trace, id="x", version=1, name="x", description="x", app=_target(),
        settle=Settle(timeout_ms=1000, poll_ms=50), max_duration_ms=60000,
        outputs={"balance": OutputSpec(type="string")}, provenance=_provenance(),
    )
    assert artifact.success.checkpoint.name == "Type"


def test_compile_fallback_prefers_a_cell_over_a_nearer_control() -> None:
    nodes = [node("cell", "Type", index=0), node("cell", "89,410.22", index=1),
             node("button", "Select", index=2)]
    artifact = _compile_with_final([_fill_step(0, "12345", "Member ID"), _read_balance()],
                                   nodes, 1)
    assert artifact.success.checkpoint.role == "cell"


def test_compile_fallback_uses_a_control_only_when_nothing_else_qualifies() -> None:
    nodes = [node("cell", "89,410.22", index=0), node("link", "Print statement", index=1),
             node("button", "Select", index=2)]
    artifact = _compile_with_final([_fill_step(0, "12345", "Member ID"), _read_balance()],
                                   nodes, 0)
    assert artifact.success.checkpoint.role in {"link", "button"}
    assert artifact.success.checkpoint.name == "Print statement"


def test_compile_refuses_when_no_value_independent_node_exists() -> None:
    nodes = [node("cell", "89,410.22", index=0), node("cell", "Acct 7431", index=1),
             node("cell", "Member 12345", index=2)]
    with pytest.raises(CompileError, match="value-independent"):
        _compile_with_final([_fill_step(0, "12345", "Member ID"), _read_balance()], nodes, 0)
