"""The `Surface` protocol: what any concrete surface must implement.

Pure `Protocol` definition -- no Playwright, no browser concept of any kind, imported
here. Only `cua/surface/web.py` may import a browser driver (`tests/test_architecture.py`
enforces the boundary for the whole system); this module is what phase 3's compiler and
phase 4's replay engine import when they need to talk about "a surface" without caring
which concrete implementation backs it.
"""

from __future__ import annotations

from typing import Protocol

from cua.surface.models import Action, ActionResult, EvidenceFrame, Locator, Observation, Resolution


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
