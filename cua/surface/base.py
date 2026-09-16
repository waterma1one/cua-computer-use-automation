"""The `Surface` protocol: what any concrete surface must implement.

Pure `Protocol` definition -- no Playwright, no browser concept of any kind, imported
here. Only `cua/surface/web.py` may import a browser driver (`tests/test_architecture.py`
enforces the boundary for the whole system); this module is what phase 3's compiler and
phase 4's replay engine import when they need to talk about "a surface" without caring
which concrete implementation backs it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from cua.surface.models import (
    Action,
    ActionResult,
    EvidenceFrame,
    Locator,
    Observation,
    Resolution,
)


class StaleObservationError(RuntimeError):
    """Raised by a surface's discovery-time entry point when a caller names a generation
    that does not match the surface's current one -- either an old generation (the page
    has since changed under it) or a generation that has never existed (a fabricated or
    future value).

    Spec §3.1: without this check, a model acting on an index from an observation that no
    longer reflects the page would click something it never actually saw. Lives here, not
    in `cua/surface/web.py`, so a caller that must not import Playwright (phase 3's
    compiler, phase 4's replay engine) can still catch it.
    """


class SurfaceError(RuntimeError):
    """A concrete `Surface` implementation's own failure, translated at its boundary.

    No implementation detail of a concrete surface -- a browser-driver exception type,
    above all -- may escape past this module. `cua/surface/web.py` is the only file
    allowed to import Playwright; if it let a raw Playwright exception propagate, every
    caller (including phase 3 and phase 4, which may not import Playwright at all) would
    have no importable type to catch it with. A concrete surface wraps its own failures
    in this instead.
    """


# Phase 5 / E4, E17: what a concrete surface consults before letting the page reach a URL.
# `None` means permitted; a string is the reason it is not. A guard that raises is treated
# as a denial (fail closed). Declared here, as a bare callable, so the surface never has to
# import `cua.policy` -- `cua.policy.allowlist.navigation_guard` builds one and the caller
# hands it over. The protocol method that reports a violation is Task 4's.
NavigationGuard = Callable[[str], str | None]


@runtime_checkable
class Surface(Protocol):
    """Perceives and acts on one application, one frame tree at a time.

    `@runtime_checkable` so `isinstance(some_surface, Surface)` is a meaningful conformance
    check (I7) -- it verifies every method below is present on the instance, which is what
    catches a structural drift (a renamed or removed method) without any caller ever naming
    a concrete surface type in an annotation.
    """

    def observe(self) -> Observation:
        """Snapshots every frame and returns the current, budget-capped Observation."""
        ...

    def act(self, action: Action) -> ActionResult:
        """Executes one Action via its `locator`. The replay-time entry point."""
        ...

    def resolve(self, locator: Locator) -> Resolution:
        """Resolves `locator` against live, freshly-observed truth."""
        ...

    def capture(self) -> EvidenceFrame:
        """Captures a scrubbed evidence frame (screenshot plus raw snapshot YAML)."""
        ...

    def pending_dialog(self) -> str | None:
        """The bare message of a currently pending native dialog, or `None` if none is
        pending.

        Additive (E6): the replay engine's settle loop polls this ahead of `observe()` on
        every iteration, since a pending dialog blocks the page and would otherwise never
        show up in an accessibility-tree snapshot. Returns just the dialog's own message --
        not a longer explanatory sentence -- so a caller can match it against a `Matcher`'s
        `name`/`name_match` directly.
        """
        ...

    def act_on_index(self, generation: int, index: int, action: str) -> ActionResult:
        """The discovery-time entry point (spec §3.1): acts on a node named by index from a
        previously returned Observation, rather than a caller-built Locator.

        `generation` must match the generation of the Observation `index` was drawn from; a
        mismatch (a stale index from a page that has since changed, or a fabricated future
        generation) is rejected -- by raising `StaleObservationError` -- rather than
        executed. R25: this stale-index rejection is a promise the protocol itself makes,
        not an implementation detail of any one concrete surface, so phase 3's compiler and
        phase 4's replay engine can type against `Surface` for this too, without ever
        importing `cua.surface.web` (the only module allowed to import Playwright).
        """
        ...

    def allowlist_violation(self) -> str | None:
        """The reason the deployment allowlist refused an application-initiated navigation
        on this surface, or `None` if none has occurred. Sticky: the first violation is the
        one reported for the life of the surface, and after it `act`/`act_on_index` raise
        `SurfaceError` (the session is frozen -- spec §6.1, criterion 2) while `observe`,
        `capture` and `pending_dialog` keep working so the failure can be evidenced.

        Phase 5 / E3: the replay engine checks this after every `act`, after every step's
        settle, and after the checkpoint settle -- three points, ahead of any other
        interpretation of what the surface returned or raised.

        A violation is guaranteed to be visible only after a subsequent blocking surface
        call. A concrete surface's `fill`/`select` do not themselves wait for a navigation
        an input triggers (e.g. an `onchange` handler that submits a form) -- Playwright's
        own default for those actions -- so a hop like that may not be recorded until the
        next `observe()` or `act()` actually blocks on it.
        """
        ...
