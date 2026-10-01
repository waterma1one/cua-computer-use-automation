"""Declared inputs reach the model as names and `{{name}}` placeholders, never as a sensitive
value; the loop substitutes the real value only on its way to the surface."""
from __future__ import annotations

from pathlib import Path

from cua.agent.loop import DeclaredInput, discover
from cua.artifact.compile import compile as compile_artifact
from cua.artifact.models import (
    App,
    FromInput,
    InputSpec,
    LiteralValue,
    Provenance,
    Settle,
)
from cua.llm.base import Message, ToolCall
from cua.llm.fake import FakeClient
from cua.observability.evidence import EvidenceWriter
from cua.surface.models import Observation
from tests.agent.conftest import FakeSurface, node
from tests.agent.test_loop import _policy, _target

SECRET = "hunter2-s3cret"
USER = "teller01"


def _declared() -> dict[str, DeclaredInput]:
    return {
        "user": DeclaredInput(spec=InputSpec(type="string"), example_value=USER),
        "password": DeclaredInput(spec=InputSpec(type="string", sensitive=True),
                                  example_value=SECRET),
    }


def _frame():  # type: ignore[no-untyped-def]
    return [node("textbox", "Username", index=0), node("textbox", "Password", index=1),
            node("button", "Sign in", index=2)]


def _changing_frames():  # type: ignore[no-untyped-def]
    """One distinct frame per action, so the loop never reads the page as stuck."""
    return [[node("textbox", "Username", index=0), node("textbox", "Password", index=1),
             node("button", f"Sign in {n}", index=2)] for n in range(5)]


def _run(script: list[ToolCall], surface: FakeSurface, **kw):  # type: ignore[no-untyped-def]
    return discover("Log in.", _target(), surface, _policy(), FakeClient(script=script),
                    declared_inputs=_declared(), **kw)


def test_placeholder_is_substituted_before_the_surface_is_called() -> None:
    surface = FakeSurface(frames=[_frame()])
    trace = _run([
        ToolCall(id="1", name="fill", args={"index": 0, "value": "{{user}}"}),
        ToolCall(id="2", name="fill", args={"index": 1, "value": "{{password}}"}),
        ToolCall(id="3", name="finish", args={"summary": "in", "checkpoint_index": 2}),
    ], surface)
    assert [c[1:] for c in surface.act_on_index_calls] == [
        (0, "fill", USER), (1, "fill", SECRET)]
    assert trace.stop_reason == "finish"


def test_messages_sent_to_the_model_never_contain_the_secret() -> None:
    seen: list[list[Message]] = []

    class Spy(FakeClient):
        def step(self, messages, tools):  # type: ignore[no-untyped-def]
            seen.append(list(messages))
            return super().step(messages, tools)

    surface = FakeSurface(frames=[_frame()], act_read_value=SECRET)
    script = [
        ToolCall(id="1", name="fill", args={"index": 1, "value": "{{password}}"}),
        ToolCall(id="2", name="finish", args={"summary": "in", "checkpoint_index": 2}),
    ]
    discover("Log in.", _target(), surface, _policy(), Spy(script=script),
             declared_inputs=_declared())
    flat = "\n".join(m.text for batch in seen for m in batch)
    assert SECRET not in flat
    assert "{{password}}" in flat


def test_surface_error_text_containing_the_secret_is_masked_for_the_model() -> None:
    from cua.surface.base import SurfaceError

    seen: list[list[Message]] = []

    class Spy(FakeClient):
        def step(self, messages, tools):  # type: ignore[no-untyped-def]
            seen.append(list(messages))
            return super().step(messages, tools)

    surface = FakeSurface(frames=[_frame()],
                          raise_on_act_on_index=SurfaceError(f"could not type {SECRET}"))
    script = [ToolCall(id="1", name="fill", args={"index": 1, "value": "{{password}}"}),
              ToolCall(id="2", name="give_up", args={"reason": "x"})]
    discover("Log in.", _target(), surface, _policy(), Spy(script=script),
             declared_inputs=_declared())
    assert SECRET not in "\n".join(m.text for batch in seen for m in batch)


def test_undeclared_placeholder_is_a_failed_turn_with_a_clear_message() -> None:
    seen: list[list[Message]] = []

    class Spy(FakeClient):
        def step(self, messages, tools):  # type: ignore[no-untyped-def]
            seen.append(list(messages))
            return super().step(messages, tools)

    surface = FakeSurface(frames=[_frame()])
    script = [ToolCall(id="1", name="fill", args={"index": 0, "value": "{{nope}}"}),
              ToolCall(id="2", name="give_up", args={"reason": "x"})]
    trace = discover("Log in.", _target(), surface, _policy(), Spy(script=script),
                     declared_inputs=_declared())
    assert surface.act_on_index_calls == []
    assert trace.steps == []
    tool_msgs = [m.text for m in seen[-1] if m.role == "tool"]
    assert any("nope" in t and "not a declared input" in t for t in tool_msgs)
    assert "user" in tool_msgs[0] and "password" in tool_msgs[0]


def test_three_undeclared_placeholders_stop_as_consecutive_failures() -> None:
    surface = FakeSurface(frames=[_frame()])
    trace = _run([ToolCall(id=str(i), name="fill", args={"index": 0, "value": "{{x}}"})
                  for i in range(3)], surface)
    assert trace.stop_reason == "consecutive_failures"


def test_a_placeholder_embedded_in_longer_text_is_malformed() -> None:
    surface = FakeSurface(frames=[_frame()])
    trace = _run([
        ToolCall(id="1", name="fill", args={"index": 1, "value": "x{{password}}"}),
        ToolCall(id="2", name="give_up", args={"reason": "x"}),
    ], surface)
    assert surface.act_on_index_calls == []
    assert trace.stop_reason == "give_up"


def _compile(trace):  # type: ignore[no-untyped-def]
    return compile_artifact(
        trace, id="corebank.login", version=1, name="login", description="d",
        app=App(vendor_product="corebank-teller", variant="base", surface="web",
                entry="/teller/index.html"),
        settle=Settle(timeout_ms=1000, poll_ms=50), max_duration_ms=60000, outputs={},
        provenance=Provenance(discovered_at="2026-09-24T00:00:00", model="m",
                              policy_mode="sandbox", provider_retention="training_permitted",
                              run_id="run-20260924000000-abcd",
                              trace_ref="evidence/run-20260924000000-abcd/trace.jsonl"))


def test_loop_then_compile_promotes_placeholders_to_from_input(tmp_path: Path) -> None:
    surface = FakeSurface(frames=_changing_frames(), advance_on_act=True)
    writer = EvidenceWriter(tmp_path)  # deliberately given no secrets to mask
    trace = _run([
        ToolCall(id="1", name="fill", args={"index": 0, "value": "{{user}}"}),
        ToolCall(id="2", name="fill", args={"index": 1, "value": "{{password}}"}),
        ToolCall(id="3", name="click", args={"index": 2}),
        ToolCall(id="4", name="finish", args={"summary": "in", "checkpoint_index": 2}),
    ], surface, evidence=writer)
    artifact = _compile(trace)
    fills = [s for s in artifact.steps if s.action == "fill"]
    assert [s.value for s in fills] == [FromInput(from_input="user"),
                                        FromInput(from_input="password")]
    assert not any(isinstance(s.value, LiteralValue) for s in fills)
    assert artifact.inputs["password"].sensitive is True
    writer.write_artifact(artifact)
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert files
    for path in files:
        assert SECRET.encode() not in path.read_bytes(), path


def test_a_literal_equal_to_a_declared_value_still_promotes() -> None:
    surface = FakeSurface(frames=[_frame()])
    trace = _run([
        ToolCall(id="1", name="fill", args={"index": 0, "value": USER}),
        ToolCall(id="2", name="finish", args={"summary": "in", "checkpoint_index": 2}),
    ], surface)
    assert [s.value for s in _compile(trace).steps if s.action == "fill"] == [
        FromInput(from_input="user")]


def test_promotion_prefers_the_sensitive_input_when_two_share_a_value() -> None:
    shared = {
        "pin_hint": DeclaredInput(spec=InputSpec(type="string"), example_value="4321"),
        "pin": DeclaredInput(spec=InputSpec(type="string", sensitive=True),
                             example_value="4321"),
    }
    surface = FakeSurface(frames=[_frame()])
    trace = discover("g", _target(), surface, _policy(), FakeClient(script=[
        ToolCall(id="1", name="fill", args={"index": 1, "value": "4321"}),
        ToolCall(id="2", name="finish", args={"summary": "in", "checkpoint_index": 2}),
    ]), declared_inputs=shared)
    assert any(s.value == FromInput(from_input="pin") for s in _compile(trace).steps)
