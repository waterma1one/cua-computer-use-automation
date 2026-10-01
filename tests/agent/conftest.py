"""A scripted `Surface` for the discovery loop's own tests -- no Playwright, no page."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

from cua.surface.base import Surface
from cua.surface.locators import synthesize
from cua.surface.models import (
    Action,
    ActionResult,
    EvidenceFrame,
    Locator,
    Node,
    NodeState,
    Observation,
    Resolution,
    SurfaceSegment,
)

PATH = [SurfaceSegment(kind="window", name="main")]


@dataclass
class VirtualClock:
    """Advances only when slept on -- the loop's post-action settle waits in virtual time."""

    now_ms: int = 0

    def monotonic_ms(self) -> int:
        return self.now_ms

    def sleep_ms(self, ms: int) -> None:
        self.now_ms += ms


@pytest.fixture(autouse=True)
def _virtual_default_clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A test that passes no `clock` gets a virtual one, so settling never really sleeps."""
    monkeypatch.setattr("cua.agent.loop.SYSTEM_CLOCK", VirtualClock())
    yield


def node(role: str, name: str | None = None, value: str | None = None, index: int = 0) -> Node:
    return Node(index=index, role=role, name=name, value=value, state=NodeState(),
                surface_path=PATH)


@dataclass
class FakeSurface:
    """`frames` is the sequence of node-lists each `observe()` call walks through, one per
    call, holding the last one once exhausted -- same convention as
    `tests/replay/conftest.py::FakeSurface`. `act_on_index`/`act`/`expand` all record what
    they were asked to do, for assertions; `raw_snapshot()` returns the same frame
    `observe()` most recently returned (this double has no separate filtered/unfiltered
    distinction -- `WebSurface`'s own tests in Task 1 cover that split for real).
    `raise_on_act_on_index`, when set, makes `act_on_index` raise instead of returning.
    `observe_errors` maps a 1-based `observe()` call number to the exception that call
    raises; `raise_on_expand` makes `expand` raise (before it observes).
    """

    frames: list[list[Node]]
    act_ok: bool = True
    act_read_value: str | None = None
    violation: str | None = None
    raise_on_act_on_index: Exception | None = None
    raise_on_expand: Exception | None = None
    observe_errors: dict[int, Exception] = field(default_factory=dict)
    observe_calls: int = 0
    generation: int = 0
    expand_calls: int = 0
    act_calls: list[Action] = field(default_factory=list)
    act_on_index_calls: list[tuple[int, int, str, str | None]] = field(default_factory=list)
    advance_on_act: bool = False
    acts: int = 0
    shown: int = 0

    def _current(self) -> list[Node]:
        if self.advance_on_act:
            return self.frames[self.shown]
        return self.frames[min(max(self.generation - 1, 0), len(self.frames) - 1)]

    def observe(self) -> Observation:
        self.observe_calls += 1
        if self.observe_calls in self.observe_errors:
            raise self.observe_errors[self.observe_calls]
        source = self.acts if self.advance_on_act else self.generation
        i = min(source, len(self.frames) - 1)
        self.shown = i
        self.generation += 1
        return Observation(generation=self.generation, nodes=self.frames[i], truncated=False)

    def act(self, action: Action) -> ActionResult:
        self.acts += 1
        self.act_calls.append(action)
        return ActionResult(ok=self.act_ok, action=action, read_value=self.act_read_value)

    def resolve(self, locator: Locator) -> Resolution:
        raise NotImplementedError

    def capture(self) -> EvidenceFrame:
        return EvidenceFrame(generation=self.generation, image_png=None, snapshot_yaml="")

    def pending_dialog(self) -> str | None:
        return None

    def act_on_index(
        self, generation: int, index: int, action: str, value: str | None = None,
    ) -> ActionResult:
        self.act_on_index_calls.append((generation, index, action, value))
        if self.raise_on_act_on_index is not None:
            raise self.raise_on_act_on_index
        current = self._current()
        self.acts += 1
        locator = synthesize(current[index], current)
        return ActionResult(
            ok=self.act_ok,
            action=Action(kind=action, locator=locator, value=value),  # type: ignore[arg-type]
            read_value=self.act_read_value,
        )

    def expand(self) -> Observation:
        self.expand_calls += 1
        if self.raise_on_expand is not None:
            raise self.raise_on_expand
        return self.observe()

    def raw_snapshot(self) -> list[Node]:
        return self._current()

    def allowlist_violation(self) -> str | None:
        return self.violation


assert isinstance(FakeSurface([]), Surface)  # module import time: conformance
