"""Spec §8.3 steps 1-6, offline -- no browser, no surface, just a Trace already captured by
Task 5's loop. The scenario throughout is the same member-search-then-read-balance flow every
other phase's fixtures use.
"""
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
    # last step's own observation here purely as a convenient stand-in (this helper's callers
    # never assert on checkpoint contents); it is deliberately not what discover() itself would
    # produce -- see the Trace docstring.
    return Trace(goal="find member and read balance", target=_target(),
                declared_inputs=declared_inputs or {}, steps=steps, stop_reason=stop_reason,
                stop_detail="done",
                checkpoint_index=checkpoint_index if stop_reason == "finish" else None,
                final_observation=steps[-1].observation if steps and stop_reason == "finish"
                else None)


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
    assert artifact.steps[0].value == FromInput(from_input="member_id")
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
    assert artifact.steps[0].value == LiteralValue(literal="some text")


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
    assert artifact.steps[1].into == "balance"
    assert artifact.steps[2].into == "_s3"


def test_compile_derives_a_next_step_lookahead_expect_and_leaves_the_last_step_empty() -> None:
    steps = [_fill_step(0, "12345", "Member ID"), _read_step(0, "Savings", "1234.56", "balance")]
    artifact = compile_artifact(
        _trace(steps, declared_inputs={"member_id": DeclaredInput(
            spec=InputSpec(type="string"), example_value="12345")}),
        id="x", version=1, name="x", description="x", app=_target(),
        settle=Settle(timeout_ms=1000, poll_ms=50), max_duration_ms=60000,
        outputs={"balance": OutputSpec(type="string")}, provenance=_provenance(),
    )
    assert len(artifact.steps[0].expects) == 1
    assert artifact.steps[0].expects[0].source == "observed"
    assert artifact.steps[0].expects[0].verified is True
    assert artifact.steps[1].expects == []


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
    assert len(artifact.steps) == 1
    assert artifact.steps[0].locator is not None and artifact.steps[0].locator.name == "Checking"


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
    assert artifact.steps[0].into == "account_balance"


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
    assert artifact.steps[0].id == "s1"
    assert artifact.steps[0].into == "_s1"
    assert artifact.steps[1].value == FromStep(from_step="s1")
