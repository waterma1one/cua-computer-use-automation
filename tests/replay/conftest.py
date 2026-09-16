from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

from cua.replay.engine import _reset_idempotency_keys
from cua.surface.base import Surface
from cua.surface.locators import resolve_against
from cua.surface.models import (
    Action,
    ActionResult,
    EvidenceFrame,
    Locator,
    Node,
    NodeState,
    Observation,
    Resolution,
)
from tests.artifact.factories import PATH


def node(role: str, name: str | None = None, value: str | None = None, index: int = 0,
        disabled: bool = False) -> Node:
    return Node(index=index, role=role, name=name, value=value,
                state=NodeState(disabled=disabled), surface_path=PATH)


@dataclass
class FakeSurface:
    """A `Surface` scripted entirely off in-memory frames -- no Playwright, no page.

    `frames` is the sequence of node-lists `observe()` walks through, one per call, holding
    the last frame once exhausted. `dialog_messages` does the same for `pending_dialog()`.
    `act_ok`/`act_read_value` script `act()`'s `ActionResult`; `raise_on_act`, when set, makes
    `act()` raise instead of returning (used for the SESSION_LOST producer test).
    """

    frames: list[list[Node]]
    dialog_messages: list[str | None] = field(default_factory=lambda: [None])
    act_ok: bool = True
    act_read_value: str | None = None
    raise_on_act: Exception | None = None
    violation: str | None = None
    dismiss_calls: int = 0
    _obs_index: int = 0
    _dialog_index: int = 0

    def observe(self) -> Observation:
        i = min(self._obs_index, len(self.frames) - 1)
        self._obs_index += 1
        return Observation(generation=self._obs_index, nodes=self.frames[i], truncated=False)

    def resolve(self, locator: Locator) -> Resolution:
        i = min(max(self._obs_index - 1, 0), len(self.frames) - 1)
        return resolve_against(locator, self.frames[i])

    def act(self, action: Action) -> ActionResult:
        if self.raise_on_act is not None:
            raise self.raise_on_act
        if action.kind == "dismiss_dialog":
            self.dismiss_calls += 1
        return ActionResult(ok=self.act_ok, action=action, read_value=self.act_read_value)

    def capture(self) -> EvidenceFrame:
        return EvidenceFrame(generation=self._obs_index, image_png=None, snapshot_yaml="")

    def act_on_index(self, generation: int, index: int, action: str) -> ActionResult:
        raise NotImplementedError("FakeSurface is replay-only")

    def pending_dialog(self) -> str | None:
        i = min(self._dialog_index, len(self.dialog_messages) - 1)
        self._dialog_index += 1
        return self.dialog_messages[i]

    def allowlist_violation(self) -> str | None:
        return self.violation


assert isinstance(FakeSurface([]), Surface)  # module import time: conformance, not just shape


@dataclass
class FakeClock:
    """Advances only when told to. `settle()` calls `sleep_ms` between polls; this records the
    call and advances virtual time without ever invoking real `time.sleep`.
    """

    _now_ms: int = 0
    sleep_calls: list[int] = field(default_factory=list)

    def monotonic_ms(self) -> int:
        return self._now_ms

    def sleep_ms(self, ms: int) -> None:
        self.sleep_calls.append(ms)
        self._now_ms += ms


@pytest.fixture(autouse=True)
def _fresh_idempotency_keys() -> Iterator[None]:
    """E27: the engine's seen-set of burned idempotency keys is process-lifetime state. Every
    test starts from an empty set so a key one test burns can never refuse another test's
    replay, whatever order the tests run in.
    """
    _reset_idempotency_keys()
    yield
    _reset_idempotency_keys()
