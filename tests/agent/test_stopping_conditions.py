"""Spec §8.1: five stopping conditions, each recording its own distinct reason."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from cua.agent.loop import DiscoveryLimits, discover
from cua.llm.base import Completion, Message, ToolCall, ToolDef
from cua.llm.fake import FakeClient
from cua.llm.gemini import LLMError
from tests.agent.conftest import FakeSurface, node
from tests.agent.test_loop import _policy, _target


@dataclass
class FakeClock:
    _now_ms: int = 0

    def monotonic_ms(self) -> int:
        return self._now_ms

    def sleep_ms(self, ms: int) -> None:
        self._now_ms += ms


@dataclass
class FlakyClient:
    """Raises `LLMError` for the first `failures` calls, then plays `script` like
    `FakeClient` -- the live API's observed transient empty-body 404s, in miniature."""

    failures: int
    script: list[ToolCall | Completion]
    calls: int = 0
    _next: int = field(default=0, repr=False)

    def step(self, messages: list[Message], tools: list[ToolDef]) -> ToolCall | Completion:
        self.calls += 1
        if self.calls <= self.failures:
            raise LLMError("transient provider failure")
        result = self.script[self._next]
        self._next += 1
        return result


def test_max_steps_reached() -> None:
    surface = FakeSurface(frames=[[node("button", "X", index=0)]])
    llm = FakeClient(script=[ToolCall(id=str(i), name="click", args={"index": 0})
                             for i in range(3)])
    trace = discover("goal", _target(), surface, _policy(), llm,
                     limits=DiscoveryLimits(max_steps=3))
    assert trace.stop_reason == "max_steps"
    assert len(trace.steps) == 3


def test_max_duration_exceeded() -> None:
    clock = FakeClock()
    surface = FakeSurface(frames=[[node("button", "X", index=0)]])

    class Advancing:
        def step(self, messages: list[Message], tools: list[ToolDef]) -> ToolCall | Completion:
            clock._now_ms += 1000
            return ToolCall(id="1", name="click", args={"index": 0})

    trace = discover("goal", _target(), surface, _policy(), Advancing(),
                     limits=DiscoveryLimits(max_duration_ms=500), clock=clock)
    assert trace.stop_reason == "max_duration"
    assert len(trace.steps) == 1


def test_dead_end_detected_on_a_repeating_digest() -> None:
    same_frame = [node("button", "X", index=0)]
    surface = FakeSurface(frames=[same_frame])  # every observe() returns the same thing
    llm = FakeClient(script=[ToolCall(id=str(i), name="click", args={"index": 0})
                             for i in range(10)])
    trace = discover("goal", _target(), surface, _policy(), llm,
                     limits=DiscoveryLimits(dead_end_repeats=3, max_steps=10))
    assert trace.stop_reason == "dead_end"
    assert len(trace.steps) == 3


def test_a_changing_observation_is_not_a_dead_end() -> None:
    frames = [[node("button", "X", index=0), node("cell", str(i), index=1)] for i in range(6)]
    surface = FakeSurface(frames=frames, advance_on_act=True)  # each click changes the page
    llm = FakeClient(script=[ToolCall(id=str(i), name="click", args={"index": 0})
                             for i in range(5)])
    trace = discover("goal", _target(), surface, _policy(), llm,
                     limits=DiscoveryLimits(dead_end_repeats=2, max_steps=5))
    assert trace.stop_reason == "max_steps"


def test_reading_several_values_from_a_static_page_is_not_a_dead_end() -> None:
    frame = [node("cell", "A", index=0), node("cell", "B", index=1)]
    surface = FakeSurface(frames=[frame])  # a results page that never changes
    llm = FakeClient(script=[
        *[ToolCall(id=str(i), name="read", args={"index": i % 2}) for i in range(5)],
        ToolCall(id="f", name="finish", args={"summary": "done", "checkpoint_index": 0}),
    ])
    trace = discover("goal", _target(), surface, _policy(), llm,
                     limits=DiscoveryLimits(dead_end_repeats=3, max_steps=10))
    assert trace.stop_reason == "finish"
    assert len(trace.steps) == 5


def test_a_read_neither_advances_nor_resets_the_unchanged_counter() -> None:
    surface = FakeSurface(frames=[[node("button", "X", index=0)]])
    llm = FakeClient(script=[
        ToolCall(id="1", name="click", args={"index": 0}),
        ToolCall(id="2", name="read", args={"index": 0}),
        ToolCall(id="3", name="click", args={"index": 0}),
        ToolCall(id="4", name="read", args={"index": 0}),
        ToolCall(id="5", name="click", args={"index": 0}),
        ToolCall(id="6", name="click", args={"index": 0}),
    ])
    trace = discover("goal", _target(), surface, _policy(), llm,
                     limits=DiscoveryLimits(dead_end_repeats=3, max_steps=10))
    assert trace.stop_reason == "dead_end"
    assert len(trace.steps) == 5  # the three clicks after the first count; reads are skipped


def test_repeated_expands_that_reveal_nothing_new_reach_dead_end() -> None:
    surface = FakeSurface(frames=[[node("button", "X", index=0)]])
    llm = FakeClient(script=[ToolCall(id=str(i), name="expand", args={}) for i in range(10)])
    trace = discover("goal", _target(), surface, _policy(), llm,
                     limits=DiscoveryLimits(dead_end_repeats=3, max_steps=10))
    assert trace.stop_reason == "dead_end"
    assert surface.expand_calls == 3


def test_consecutive_malformed_tool_calls() -> None:
    surface = FakeSurface(frames=[[node("button", "X", index=0)]])
    llm = FakeClient(script=[
        ToolCall(id="1", name="not_a_real_tool", args={}),
        Completion(text="I don't know what to do"),
        ToolCall(id="2", name="click", args={"wrong_arg": True}),
    ])
    trace = discover("goal", _target(), surface, _policy(), llm,
                     limits=DiscoveryLimits(max_consecutive_failures=3))
    assert trace.stop_reason == "consecutive_failures"
    assert trace.steps == []


def test_a_successful_action_between_two_malformed_ones_resets_the_counter() -> None:
    surface = FakeSurface(frames=[[node("button", "X", index=0)]] * 3)
    llm = FakeClient(script=[
        ToolCall(id="1", name="bogus", args={}),
        ToolCall(id="2", name="click", args={"index": 0}),
        ToolCall(id="3", name="bogus", args={}),
        ToolCall(id="4", name="give_up", args={"reason": "enough"}),
    ])
    trace = discover("goal", _target(), surface, _policy(), llm,
                     limits=DiscoveryLimits(max_consecutive_failures=2))
    assert trace.stop_reason == "give_up"


@pytest.mark.parametrize("args", [
    {"index": "0"},
    {"index": True},
    {"index": 2.5},
    {"index": None},
    {"index": 5},
    {"index": -1},
])
def test_a_badly_typed_or_out_of_range_index_is_malformed_and_never_reaches_the_surface(
    args: dict[str, object],
) -> None:
    surface = FakeSurface(frames=[[node("button", "X", index=0)]])
    llm = FakeClient(script=[ToolCall(id="1", name="click", args=args)])
    trace = discover("goal", _target(), surface, _policy(), llm,
                     limits=DiscoveryLimits(max_consecutive_failures=1))
    assert trace.stop_reason == "consecutive_failures"
    assert surface.act_on_index_calls == []
    assert surface.act_calls == []


@pytest.mark.parametrize("call", [
    ToolCall(id="1", name="fill", args={"index": 0, "value": 12345}),
    ToolCall(id="1", name="navigate", args={"path": ["/x"]}),
    ToolCall(id="1", name="read", args={"index": 0, "output_name": 3}),
    ToolCall(id="1", name="finish", args={"summary": "done", "checkpoint_index": "0"}),
    ToolCall(id="1", name="finish", args={"summary": "done", "checkpoint_index": 9}),
    ToolCall(id="1", name="finish", args={"summary": None, "checkpoint_index": 0}),
    ToolCall(id="1", name="give_up", args={"reason": 7}),
])
def test_a_badly_typed_argument_is_a_malformed_turn(call: ToolCall) -> None:
    surface = FakeSurface(frames=[[node("textbox", "Member ID", index=0)]])
    llm = FakeClient(script=[call])
    trace = discover("goal", _target(), surface, _policy(), llm,
                     limits=DiscoveryLimits(max_consecutive_failures=1))
    assert trace.stop_reason == "consecutive_failures"
    assert surface.act_on_index_calls == []
    assert surface.act_calls == []


def test_an_integral_float_index_is_accepted_and_recorded_as_an_int() -> None:
    surface = FakeSurface(frames=[[node("button", "X", index=0)]])
    llm = FakeClient(script=[
        ToolCall(id="1", name="click", args={"index": 0.0}),
        ToolCall(id="2", name="finish", args={"summary": "ok", "checkpoint_index": 0.0}),
    ])
    trace = discover("goal", _target(), surface, _policy(), llm)
    assert trace.stop_reason == "finish"
    assert surface.act_on_index_calls[0][1] == 0
    recorded = trace.steps[0].tool_call.args["index"]
    assert isinstance(recorded, int)
    assert trace.checkpoint_index == 0


def test_a_transient_llm_error_is_a_failed_turn_not_a_crash() -> None:
    surface = FakeSurface(frames=[[node("button", "X", index=0)]])
    llm = FlakyClient(failures=2, script=[ToolCall(id="1", name="give_up", args={"reason": "x"})])
    trace = discover("goal", _target(), surface, _policy(), llm,
                     limits=DiscoveryLimits(max_consecutive_failures=3))
    assert trace.stop_reason == "give_up"
    assert llm.calls == 3


def test_persistent_llm_errors_stop_on_consecutive_failures() -> None:
    clock = FakeClock()
    surface = FakeSurface(frames=[[node("button", "X", index=0)]])
    llm = FlakyClient(failures=10, script=[])
    trace = discover("goal", _target(), surface, _policy(), llm,
                     limits=DiscoveryLimits(max_consecutive_failures=3), clock=clock)
    assert trace.stop_reason == "consecutive_failures"
    assert "transient provider failure" in trace.stop_detail
    assert llm.calls == 3
    assert clock._now_ms == 0  # no sleeping, no backoff inside the loop
