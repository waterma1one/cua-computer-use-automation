"""After a state-changing action the loop must not adopt the page it observes first: a
click that triggers a navigation returns before the navigation commits, so the first
observations can still show the old page. The loop settles by polling `observe()` through
the injected `Clock` until the page has changed and holds still, or a bounded timeout.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from cua.agent.loop import DiscoveryLimits, Trace, discover
from cua.llm.base import ToolCall
from cua.llm.fake import FakeClient
from cua.surface.models import ActionResult, Node, Observation
from tests.agent.conftest import FakeSurface, node
from tests.agent.test_loop import _policy, _target

LOGIN = [node("textbox", "Username", index=0), node("textbox", "Password", index=1),
         node("button", "Sign in", index=2)]
HOME = [node("heading", "Member Search", index=0), node("textbox", "Member ID", index=1),
        node("button", "Search", index=2)]


@dataclass
class FakeClock:
    now_ms: int = 0
    sleeps: list[int] = field(default_factory=list)

    def monotonic_ms(self) -> int:
        return self.now_ms

    def sleep_ms(self, ms: int) -> None:
        self.sleeps.append(ms)
        self.now_ms += ms


@dataclass
class SlowNavigationSurface(FakeSurface):
    """Shows `before` until a click, then keeps showing `before` for the next
    `stale_observes` observations (the navigation has not committed yet), then `after`."""

    before: list[Node] = field(default_factory=list)
    after: list[Node] = field(default_factory=list)
    stale_observes: int = 0
    clicked: bool = False
    observes_since_click: int = 0
    last: list[Node] = field(default_factory=list)

    def observe(self) -> Observation:
        self.observe_calls += 1
        self.generation += 1
        if not self.clicked:
            nodes = self.before
        else:
            self.observes_since_click += 1
            nodes = self.before if self.observes_since_click <= self.stale_observes else self.after
        self.last = nodes
        return Observation(generation=self.generation, nodes=nodes, truncated=False)

    def act_on_index(
        self, generation: int, index: int, action: str, value: str | None = None,
    ) -> ActionResult:
        result = super().act_on_index(generation, index, action, value)
        if action == "click":
            self.clicked = True
        return result

    def _current(self) -> list[Node]:
        return self.last


def _run(stale_observes: int, clock: FakeClock) -> tuple[Trace, FakeClient]:
    surface = SlowNavigationSurface(frames=[LOGIN], before=LOGIN, after=HOME,
                                    stale_observes=stale_observes)
    llm = FakeClient(script=[
        ToolCall(id="1", name="click", args={"index": 2}),
        ToolCall(id="2", name="finish", args={"summary": "logged in", "checkpoint_index": 0}),
    ])
    trace = discover("Log in.", _target(), surface, _policy(), llm, clock=clock)
    return trace, llm


def _last_seen(llm: FakeClient) -> str:
    return llm.calls[-1][0][-1].text or ""


def test_a_click_whose_navigation_commits_late_is_observed_after_it_commits() -> None:
    for stale in (1, 2, 5):
        clock = FakeClock()
        trace, llm = _run(stale, clock)
        assert trace.stop_reason == "finish"
        final = trace.final_observation
        assert final is not None
        assert [n.name for n in final.nodes] == ["Member Search", "Member ID", "Search"], stale
        assert "Member Search" in _last_seen(llm)
        assert "Password" not in _last_seen(llm)
        assert clock.sleeps, "settling must wait through the injected clock"


def test_a_click_that_changes_nothing_stops_settling_at_the_bounded_timeout() -> None:
    clock = FakeClock()
    limits = DiscoveryLimits()
    trace, _ = _run(stale_observes=10_000, clock=clock)
    final = trace.final_observation
    assert final is not None
    assert [n.name for n in final.nodes] == ["Username", "Password", "Sign in"]
    assert sum(clock.sleeps) <= limits.settle_timeout_ms + limits.settle_poll_ms
    assert sum(clock.sleeps) >= limits.settle_timeout_ms


def test_a_read_does_not_wait_for_the_page_to_change() -> None:
    clock = FakeClock()
    surface = FakeSurface(frames=[[node("cell", "Savings", value="1.00", index=0)]])
    llm = FakeClient(script=[
        ToolCall(id="1", name="read", args={"index": 0, "output_name": "balance"}),
        ToolCall(id="2", name="finish", args={"summary": "read", "checkpoint_index": 0}),
    ])
    trace = discover("Read.", _target(), surface, _policy(), llm, clock=clock)
    assert trace.stop_reason == "finish"
    assert clock.sleeps == []
