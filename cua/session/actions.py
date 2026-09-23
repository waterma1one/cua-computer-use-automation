"""Spec §7.5: human-action capture. Two halves, deliberately unequal in how much they can be
trusted. `record_human_action` turns whatever the best-effort `capture.js` posted into a
trace event -- unverifiable as complete (stopPropagation, an uninstrumented frame, a canvas
control all defeat it, per the spec's own words). `HumanActionBracket` is the strong half: a
screenshot and a snapshot taken before control passes to a human and again after, through the
same `Surface.capture()`/`EvidenceSink` every other frame in this system goes through, which
means it inherits `RedactingWriter`'s pattern filter automatically -- a captured human action
is exactly the kind of credential-bearing text D42's declared-input masking does not cover,
so it must ride the general filter, never a special case.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


class _CaptureSink(Protocol):
    def event(self, **fields: object) -> None: ...
    def frame(self, frame: object, name: str) -> None: ...


class _CaptureSurface(Protocol):
    def capture(self) -> object: ...
    def pending_dialog(self) -> str | None: ...


@dataclass(frozen=True)
class HumanAction:
    """One captured human action. `human_origin` is required, not defaulted (E20) -- the real
    guarantee this codebase gives is structural, not type-level: `tests/test_architecture.py`
    greps `cua/` for `HumanAction(` and requires it to appear only in this module (specifically,
    only inside `record_human_action`, below). A defaulted `Literal[True] = True` field would
    still be an ordinary settable kwarg at every call site regardless of its annotation -- the
    contract card's own earlier draft claimed the default alone made this "never
    caller-constructible with `human_origin=False`," which is false; this is the honest version
    of that claim.
    """

    operator_id: str
    type: str
    role: str | None
    name: str | None
    value: str | None
    url: str
    timestamp: int
    human_origin: Literal[True]


def record_human_action(
    sink: _CaptureSink, operator_id: str, raw: dict[str, object]
) -> HumanAction:
    action = HumanAction(
        operator_id=operator_id, type=str(raw.get("type", "")),
        role=raw.get("role"), name=raw.get("name"),  # type: ignore[arg-type]
        value=raw.get("value"), url=str(raw.get("url", "")),  # type: ignore[arg-type]
        timestamp=int(raw.get("timestamp") or 0),  # type: ignore[call-overload]
        human_origin=True,
    )
    sink.event(
        kind="human_action", operator_id=action.operator_id, human_origin=action.human_origin,
        type=action.type, role=action.role, name=action.name, value=action.value,
        url=action.url, timestamp=action.timestamp,
    )
    return action


class HumanActionBracket:
    """Spec §7.5's discrete checkpoints around a human's active window -- captured before the
    lease transfers to the operator and again after it transfers back.
    """

    def _capture(
        self,
        surface: _CaptureSurface,
        sink: _CaptureSink,
        bracket: Literal["before", "after"],
    ) -> None:
        message = surface.pending_dialog()
        if message is not None:
            sink.event(kind="human_bracket_skipped", bracket=bracket,
                      reason="a native dialog is pending")
            return
        sink.frame(surface.capture(), f"human_{bracket}")

    def before(self, surface: _CaptureSurface, sink: _CaptureSink) -> None:
        self._capture(surface, sink, "before")

    def after(self, surface: _CaptureSurface, sink: _CaptureSink) -> None:
        self._capture(surface, sink, "after")
