"""The `Surface` protocol: what any concrete surface must implement.

Pure `Protocol` definition -- no Playwright, no browser concept of any kind, imported
here. Only `cua/surface/web.py` may import a browser driver (`tests/test_architecture.py`
enforces the boundary for the whole system); this module is what phase 3's compiler and
phase 4's replay engine import when they need to talk about "a surface" without caring
which concrete implementation backs it.
"""

from __future__ import annotations

from typing import Protocol

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


class Surface(Protocol):
    """Perceives and acts on one application, one frame tree at a time."""

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
