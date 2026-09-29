"""The discovery loop's happy path: fill member id, read balance, finish -- the same
member-search flow every other phase's fixtures use (`tests/artifact/factories.py::base()`).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from cua.agent.loop import DeclaredInput, DiscoveryLimits, discover
from cua.artifact.models import App, InputSpec
from cua.llm.base import Completion, Message, ToolCall
from cua.llm.fake import FakeClient
from cua.policy.config import PolicyConfig
from cua.surface.base import StaleObservationError, SurfaceError
from tests.agent.conftest import FakeSurface, node


def _target() -> App:
    return App(vendor_product="corebank-teller", variant="base", surface="web",
               entry="/teller/index.html")


def _policy(mode: str = "sandbox") -> PolicyConfig:
    return PolicyConfig.model_validate({
        "policy_mode": mode, "allowed_origins": ["http://x"], "allowed_paths": ["/"],
        "denied_paths": [], "allowed_actions": [
            "navigate", "click", "fill", "select", "press_key", "wait_for", "read",
            "dismiss_dialog",
        ],
    })


@dataclass
class RecordingSink:
    run_id: str = "test-run"
    events: list[dict[str, object]] = field(default_factory=list)

    def event(self, **fields: object) -> None:
        self.events.append(fields)

    def frame(self, frame: object, name: str) -> None:
        pass

    def evidence_ref(self) -> str:
        return self.run_id


def test_discover_runs_to_finish_and_produces_a_trace() -> None:
    member_id_box = node("textbox", "Member ID", index=0)
    balance = node("cell", "Savings", value="1234.56", index=1)
    surface = FakeSurface(frames=[[member_id_box, balance]])
    llm = FakeClient(script=[
        ToolCall(id="1", name="fill", args={"index": 0, "value": "12345"}),
        ToolCall(id="2", name="read", args={"index": 1, "output_name": "balance"}),
        ToolCall(id="3", name="finish",
                 args={"summary": "read the balance", "checkpoint_index": 1}),
    ])
    trace = discover(
        "Find member 12345 and read their balance.", _target(), surface, _policy(), llm,
        declared_inputs={"member_id": DeclaredInput(
            spec=InputSpec(type="string", pattern="^[0-9]{5}$"), example_value="12345")},
    )
    assert trace.stop_reason == "finish"
    assert trace.stop_detail == "read the balance"
    assert trace.checkpoint_index == 1
    assert [s.tool_call.name for s in trace.steps] == ["fill", "read"]
    assert trace.steps[0].tool_call.args["value"] == "12345"
    assert trace.steps[0].locator is not None
    assert "member_id" in trace.declared_inputs
    assert surface.act_on_index_calls[0][1:] == (0, "fill", "12345")
    assert all(not s.discovery_only and not s.human_origin and s.ok for s in trace.steps)


def test_final_observation_is_what_the_model_saw_at_finish_not_the_last_steps() -> None:
    box = node("textbox", "Member ID", index=0)
    result = node("cell", "Savings", value="1234.56", index=1)
    surface = FakeSurface(frames=[[box], [box, result]])
    llm = FakeClient(script=[
        ToolCall(id="1", name="fill", args={"index": 0, "value": "12345"}),
        ToolCall(id="2", name="finish", args={"summary": "found", "checkpoint_index": 1}),
    ])
    trace = discover("goal", _target(), surface, _policy(), llm)
    assert trace.stop_reason == "finish"
    assert len(trace.steps[-1].observation.nodes) == 1
    assert trace.final_observation is not None
    assert trace.final_observation.nodes[1].name == "Savings"
    assert trace.steps[-1].raw_nodes == [box]


def test_discover_stops_on_give_up() -> None:
    surface = FakeSurface(frames=[[node("textbox", "Member ID")]])
    llm = FakeClient(script=[ToolCall(id="1", name="give_up", args={"reason": "stuck"})])
    trace = discover("goal", _target(), surface, _policy(), llm)
    assert trace.stop_reason == "give_up"
    assert trace.stop_detail == "stuck"
    assert trace.steps == []
    assert trace.checkpoint_index is None
    assert trace.final_observation is not None


def test_expand_re_observes_and_is_never_recorded_as_a_step() -> None:
    surface = FakeSurface(frames=[[node("textbox", "Member ID")]])
    llm = FakeClient(script=[
        ToolCall(id="1", name="expand", args={}),
        ToolCall(id="2", name="give_up", args={"reason": "done looking"}),
    ])
    trace = discover("goal", _target(), surface, _policy(), llm)
    assert surface.expand_calls == 1
    assert trace.steps == []


def test_strict_mode_refuses_a_risky_action_sandbox_mode_allows_it() -> None:
    transfer_link = node("link", "Transfer History", index=0)
    surface = FakeSurface(frames=[[transfer_link]])
    llm_strict = FakeClient(script=[
        ToolCall(id="1", name="click", args={"index": 0}),
        ToolCall(id="2", name="click", args={"index": 0}),
        ToolCall(id="3", name="click", args={"index": 0}),
    ])
    trace = discover("goal", _target(), surface, _policy("strict"), llm_strict,
                     limits=DiscoveryLimits(max_consecutive_failures=3))
    assert trace.stop_reason == "consecutive_failures"
    assert trace.steps == []
    assert surface.act_on_index_calls == []

    surface2 = FakeSurface(frames=[[transfer_link]])
    llm_sandbox = FakeClient(script=[
        ToolCall(id="1", name="click", args={"index": 0}),
        ToolCall(id="2", name="finish", args={"summary": "clicked it", "checkpoint_index": 0}),
    ])
    trace2 = discover("goal", _target(), surface2, _policy("sandbox"), llm_sandbox)
    assert trace2.stop_reason == "finish"
    assert len(trace2.steps) == 1


def test_strict_mode_lets_a_safe_action_through() -> None:
    surface = FakeSurface(frames=[[node("button", "Search", index=0)]])
    llm = FakeClient(script=[
        ToolCall(id="1", name="click", args={"index": 0}),
        ToolCall(id="2", name="give_up", args={"reason": "x"}),
    ])
    trace = discover("goal", _target(), surface, _policy("strict"), llm)
    assert len(trace.steps) == 1


def test_navigate_and_dismiss_dialog_go_through_act_with_no_locator() -> None:
    surface = FakeSurface(frames=[[node("button", "Search", index=0)]])
    llm = FakeClient(script=[
        ToolCall(id="1", name="navigate", args={"path": "/teller/index.html"}),
        ToolCall(id="2", name="dismiss_dialog", args={}),
        ToolCall(id="3", name="give_up", args={"reason": "x"}),
    ])
    trace = discover("goal", _target(), surface, _policy(), llm)
    assert [a.kind for a in surface.act_calls] == ["navigate", "dismiss_dialog"]
    assert surface.act_calls[0].value == "/teller/index.html"
    assert [s.tool_call.name for s in trace.steps] == ["navigate", "dismiss_dialog"]
    assert all(s.locator is None for s in trace.steps)
    assert surface.act_on_index_calls == []


def test_each_turn_shows_the_goal_the_current_observation_and_prior_tool_results() -> None:
    surface = FakeSurface(frames=[[node("textbox", "Member ID", index=0)]])
    llm = FakeClient(script=[
        ToolCall(id="c1", name="fill", args={"index": 0, "value": "12345"}),
        ToolCall(id="c2", name="give_up", args={"reason": "x"}),
    ])
    discover("Find member 12345", _target(), surface, _policy(), llm)
    first, _ = llm.calls[0]
    second, tools = llm.calls[1]
    assert first[0].role == "system"
    assert "Find member 12345" in first[0].text
    assert first[-1].role == "user"
    assert '0: textbox "Member ID"' in first[-1].text
    # One observation per turn -- the current one -- not a growing pile of stale ones.
    assert sum(1 for m in second if m.role == "user") == 1
    tool_results = [m for m in second if m.role == "tool"]
    assert tool_results == [Message(role="tool", text="ok", tool_name="fill",
                                     tool_call_id="c1")]
    model_turns = [m for m in second if m.role == "model"]
    assert len(model_turns) == 1
    assert model_turns[0].text  # never an empty text part
    assert {t.name for t in tools} >= {"fill", "finish", "give_up", "expand"}


def test_a_failed_action_is_not_recorded_and_counts_toward_consecutive_failures() -> None:
    surface = FakeSurface(frames=[[node("button", "X", index=0)], [node("button", "Y")]],
                          act_ok=False)
    llm = FakeClient(script=[ToolCall(id=str(i), name="click", args={"index": 0})
                             for i in range(2)])
    trace = discover("goal", _target(), surface, _policy(), llm,
                     limits=DiscoveryLimits(max_consecutive_failures=2))
    assert trace.stop_reason == "consecutive_failures"
    assert trace.steps == []


@pytest.mark.parametrize("exc", [StaleObservationError("stale"), SurfaceError("boom")])
def test_a_surface_exception_is_a_failed_turn_and_the_loop_re_observes(exc: Exception) -> None:
    surface = FakeSurface(frames=[[node("button", "X", index=0)]], raise_on_act_on_index=exc)
    llm = FakeClient(script=[
        ToolCall(id="1", name="click", args={"index": 0}),
        ToolCall(id="2", name="click", args={"index": 0}),
    ])
    trace = discover("goal", _target(), surface, _policy(), llm,
                     limits=DiscoveryLimits(max_consecutive_failures=2))
    assert trace.stop_reason == "consecutive_failures"
    assert trace.steps == []
    # The second attempt names the fresh generation, not the one that went stale.
    assert surface.act_on_index_calls[0][0] < surface.act_on_index_calls[1][0]


def test_an_allowlist_violation_stops_the_run_immediately() -> None:
    surface = FakeSurface(frames=[[node("link", "Elsewhere", index=0)]],
                          violation="navigated off-allowlist")
    llm = FakeClient(script=[
        ToolCall(id="1", name="click", args={"index": 0}),
        ToolCall(id="2", name="finish", args={"summary": "x", "checkpoint_index": 0}),
    ])
    trace = discover("goal", _target(), surface, _policy(), llm)
    assert trace.stop_reason == "consecutive_failures"
    assert "allowlist violation" in trace.stop_detail
    assert trace.steps == []


def test_an_allowlist_violation_surfacing_as_a_surface_error_is_still_reported() -> None:
    surface = FakeSurface(frames=[[node("link", "Elsewhere", index=0)]],
                          violation="navigated off-allowlist",
                          raise_on_act_on_index=SurfaceError("session frozen"))
    llm = FakeClient(script=[ToolCall(id="1", name="click", args={"index": 0})])
    trace = discover("goal", _target(), surface, _policy(), llm)
    assert trace.stop_reason == "consecutive_failures"
    assert "allowlist violation" in trace.stop_detail


def test_evidence_gets_one_event_per_turn_and_a_stop_event() -> None:
    sink = RecordingSink()
    surface = FakeSurface(frames=[[node("button", "X", index=0)]])
    llm = FakeClient(script=[
        ToolCall(id="1", name="bogus", args={}),
        Completion(text="hmm"),
        ToolCall(id="2", name="click", args={"index": 0}),
        ToolCall(id="3", name="give_up", args={"reason": "x"}),
    ])
    discover("goal", _target(), surface, _policy(), llm, evidence=sink)
    kinds = [e["kind"] for e in sink.events]
    assert kinds == ["malformed_call", "malformed_call", "action", "discovery_stop"]
    assert sink.events[-1]["reason"] == "give_up"
