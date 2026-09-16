"""The deterministic settle loop: polls a surface against a step's `expects` and the
artifact's `recovery` rules until one settles, a recovery rule fires, or the step's own
`Settle` timeout is reached.

Acts only through the `Surface` protocol (D19) -- no Playwright, no Selenium, no DOM
concept, no CSS selector or XPath anywhere in this module. Every wait is a poll against
`clock`, never a fixed sleep: tests inject a `FakeClock` so a full `timeout_ms` elapses in
bounded virtual time, and `SYSTEM_CLOCK` is the only concrete `Clock` this module ships,
backed by the real `time` module for actual replay.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable

from cua.artifact.models import Expect, FailureKind, Recovery, Settle, Step
from cua.surface.base import Surface
from cua.surface.locators import matches, name_matches, text_of
from cua.surface.models import Action, Observation

__all__ = [
    "SYSTEM_CLOCK",
    "BranchOutcome",
    "Business",
    "Clock",
    "Continue",
    "DialogUnhandled",
    "Escalate",
    "Fail",
    "TimedOut",
    "settle",
]


@runtime_checkable
class Clock(Protocol):
    """The one time source `settle` ever consults. No wait in this module reads the real
    clock or calls `time.sleep` directly -- always through this, so a test can inject a
    clock that advances only when told to and never performs a real sleep.
    """

    def monotonic_ms(self) -> int:
        """A monotonically increasing millisecond timestamp."""
        ...

    def sleep_ms(self, ms: int) -> None:
        """Waits `ms` milliseconds before returning."""
        ...


@dataclass
class _SystemClock:
    """The real, `time`-backed `Clock`."""

    def monotonic_ms(self) -> int:
        return int(time.monotonic() * 1000)

    def sleep_ms(self, ms: int) -> None:
        time.sleep(ms / 1000)


SYSTEM_CLOCK: Clock = _SystemClock()


@dataclass(frozen=True)
class Continue:
    """The step settled with nothing more to report -- proceed to the next step."""


@dataclass(frozen=True)
class Business:
    """A declared, expected non-success ending matched (an `Expect` with `outcome=business`)."""

    code: str | None
    message: str


@dataclass(frozen=True)
class Fail:
    """A declared failure ending matched (an `Expect` with `outcome=fail`)."""

    matched_text: str
    code: FailureKind


@dataclass(frozen=True)
class Escalate:
    """A `Recovery` rule with `handle=escalate` fired -- a human must resolve this before
    replay can continue.
    """

    recovery_name: str


@dataclass(frozen=True)
class TimedOut:
    """No `Expect` clause and no `Recovery` rule ever matched before the step's `Settle`
    deadline. Carries the last observation taken, for diagnosis.
    """

    observation: Observation


@dataclass(frozen=True)
class DialogUnhandled:
    """A native dialog appeared and no `Recovery` rule's `detect` matched its message."""

    message: str


BranchOutcome = Continue | Business | Fail | Escalate | TimedOut | DialogUnhandled


def _handle_dialog(
    surface: Surface, message: str, recovery: list[Recovery]
) -> Escalate | DialogUnhandled | None:
    """Checks `message` against every recovery rule in order, by `name`/`name_match` only --
    a dialog message is a plain string, not a `Node`, so it goes through `name_matches`
    directly rather than the `Node`-shaped `matches` (`role`/`strategy` on the detect rule
    are simply ignored for this check). `None` means a `dismiss` rule matched and the
    dialog was dismissed; the caller must still respect `settle_spec`'s deadline for this
    (E26) rather than looping on it unboundedly.
    """
    for rule in recovery:
        detect = rule.detect
        if not name_matches(message, detect.name, detect.name_match):
            continue
        if rule.handle == "dismiss":
            surface.act(Action(kind="dismiss_dialog"))
            return None
        return Escalate(recovery_name=rule.name)
    return DialogUnhandled(message=message)


def _matching_recovery(observation: Observation, recovery: list[Recovery]) -> Escalate | None:
    """Checks `recovery` in order and stops at the first rule whose `detect` matches any
    observed node -- an `escalate` match returns immediately; a `dismiss` match is absorbed
    (there is nothing to act on for a node-based match, unlike a native dialog), stopping
    the search here rather than letting a later rule override it.
    """
    for rule in recovery:
        detect = rule.detect
        if any(matches(n, strategy=detect.strategy, role=detect.role, name=detect.name,
                        name_match=detect.name_match) for n in observation.nodes):
            if rule.handle == "escalate":
                return Escalate(recovery_name=rule.name)
            return None
    return None


def _matching_expect(
    observation: Observation, expects: list[Expect]
) -> Continue | Business | Fail | None:
    """Checks `expects` in declared order and stops at the first one whose `when` matches
    -- first match wins, even when that first match is a `retry` (which is absorbed: the
    search stops there for this poll rather than falling through to a later clause).
    """
    for expect in expects:
        when = expect.when
        matched = [
            n for n in observation.nodes
            if matches(n, strategy=when.strategy, role=when.role, name=when.name,
                       name_match=when.name_match)
        ]
        if not matched:
            continue
        if expect.outcome == "continue":
            return Continue()
        text = text_of(matched[0]) or ""
        if expect.outcome == "business":
            return Business(code=expect.code, message=text)
        if expect.outcome == "fail":
            # Safe: Task 1's load-time validation (FAIL_CODE_NOT_A_FAILURE_KIND) already
            # refuses to load any artifact with a `fail` expect whose `code` is not a valid
            # `FailureKind`, and settle()'s callers in this plan never hand it an
            # unvalidated artifact -- so this is a cast, not a `type: ignore`.
            return Fail(matched_text=text, code=cast(FailureKind, expect.code))
        # expect.outcome == "retry": absorbed -- stop searching, keep polling.
        return None
    return None


def settle(
    surface: Surface, step: Step, settle_spec: Settle, recovery: list[Recovery], *,
    clock: Clock = SYSTEM_CLOCK,
    record_dialog: Callable[[str, str], None] | None = None,
) -> BranchOutcome:
    """Polls `surface` against `step.expects` and `recovery` until one settles, a recovery
    rule escalates, an unhandled dialog appears, or `settle_spec.timeout_ms` elapses.

    Per poll: `pending_dialog()` first (a pending dialog blocks the page and would never
    show up in an ordinary observation); then `observe()` and recovery rules over the
    observed nodes; then, only if `step.expects` is non-empty, `step.expects` in declared
    order. A step with no `expects` at all completes on its very first poll -- there is
    nothing declared to wait for, so polling to timeout would be pure waste.

    E26: a dismissed dialog does not loop immediately. A dialog that keeps reappearing
    (a broken page, or a `dismiss` rule matching something that is never actually cleared)
    must still respect `settle_spec.timeout_ms` rather than dismissing without bound, so a
    dismiss falls through to the same deadline check and poll sleep as any other absorbed
    iteration below, instead of restarting the loop before either runs.

    Phase 5 / E10: when `record_dialog` is given, every dialog this loop handles is
    reported to it as `(message, handling)` -- `handling` is `"dismissed"`, `"escalated"`
    or `"unhandled"` -- so the caller's trace records a dialog as well as routing it.
    """
    deadline = clock.monotonic_ms() + settle_spec.timeout_ms
    # No observation exists yet if every poll finds a dialog pending; this placeholder
    # means `TimedOut` can always be constructed even if `observe()` is never reached.
    observation = Observation(generation=0, nodes=[], truncated=False)

    while True:
        message = surface.pending_dialog()
        if message is not None:
            dialog_outcome = _handle_dialog(surface, message, recovery)
            if record_dialog is not None:
                # E10 (phase 5): a dialog is recorded as well as routed -- spec §5.5 says
                # "recorded", and D32 only routed. The handling names what happened to it.
                handling = (
                    "dismissed" if dialog_outcome is None
                    else "escalated" if isinstance(dialog_outcome, Escalate)
                    else "unhandled"
                )
                record_dialog(message, handling)
            if dialog_outcome is not None:
                return dialog_outcome
            # A dismiss rule fired -- fall through to the deadline check and sleep below
            # (E26) rather than looping back to the top immediately.
        else:
            observation = surface.observe()

            recovery_outcome = _matching_recovery(observation, recovery)
            if recovery_outcome is not None:
                return recovery_outcome

            if not step.expects:
                return Continue()

            expect_outcome = _matching_expect(observation, step.expects)
            if expect_outcome is not None:
                return expect_outcome

        if clock.monotonic_ms() >= deadline:
            return TimedOut(observation=observation)

        clock.sleep_ms(settle_spec.poll_ms)
