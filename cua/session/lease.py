"""Spec §7.1: the control lease. Stated accurately, it is an agent-side mutex and an audit
record of intent -- a person at the keyboard can drive a live browser whether or not they
hold the token; the lease prevents the automation from acting under a human, it does not
physically prevent the human from acting. `LeasedSurface` is where that boundary actually
lives in code: it implements `Surface` itself, gates `act`/`act_on_index` on the current
holder's token, and delegates perception unconditionally, because §7.1 governs acting, not
observing.

`LeasedSurface` cannot widen the `Surface` protocol to take a token parameter on `act` --
`runtime_checkable` conformance checks method names, not exact signatures, so a wider `act`
would still satisfy `isinstance(x, Surface)` while breaking every caller that follows the
protocol's existing call convention. The token is captured at construction and re-checked
against the lease's *current* token on every gated call -- so a lease that transfers away
mid-session immediately revokes the wrapper built from the token it just invalidated.

E18 (phase 6 review): one `LeasedSurface` per SESSION, not one per acquisition -- `rebind`
updates the captured token in place, so the same object identity a long-running caller (a
session's own replay thread, holding this object deep inside an in-progress `replay()` call)
already has keeps working after the lease transfers back to the actor this wrapper was built
for. Reconstructing a *new* `LeasedSurface` on every transfer, instead, would strand the
running caller holding the old, now-permanently-stale one -- nothing outside this module would
have a way to swap it out from under a call already in progress.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Literal

from cua.surface.base import Surface, SurfaceError
from cua.surface.models import Action, ActionResult, EvidenceFrame, Locator, Observation, Resolution

__all__ = ["Controller", "Lease", "LeaseError", "LeasedSurface"]

Controller = Literal["agent", "operator", "none"]


class LeaseError(SurfaceError):
    """Raised by `LeasedSurface` when the actor it was built for no longer holds the lease."""


@dataclass
class Lease:
    """One session's control lease. `controller`/`token` are read directly by a caller that
    needs to know who holds it (the console, the escalation handler); `acquire`/`release`/
    `transfer` are the only ways to change either.
    """

    controller: Controller = "none"
    token: str | None = None

    def acquire(self, as_: Controller) -> str:
        if self.controller != "none":
            raise ValueError(f"the lease is already held by {self.controller!r}")
        self.controller = as_
        self.token = secrets.token_urlsafe(16)
        return self.token

    def release(self) -> None:
        self.controller = "none"
        self.token = None

    def transfer(self, to: Controller) -> str:
        if self.controller == "none":
            raise ValueError("the lease is not held; acquire it first")
        self.controller = to
        self.token = secrets.token_urlsafe(16)
        return self.token

    def holds(self, token: str) -> bool:
        return self.token is not None and self.token == token


class LeasedSurface:
    """A `Surface` that requires `lease.holds(token)` before `act`/`act_on_index` reach the
    underlying surface (acceptance criterion 1). `token` is the value this wrapper was built
    with at construction -- it is re-checked, not cached as "was valid once," so a lease
    transfer immediately revokes it.
    """

    def __init__(self, surface: Surface, lease: Lease, token: str) -> None:
        self._surface = surface
        self._lease = lease
        self._token = token

    def _require_lease(self) -> None:
        if not self._lease.holds(self._token):
            raise LeaseError(
                f"this actor does not hold the lease (current controller: "
                f"{self._lease.controller!r})"
            )

    def act(self, action: Action) -> ActionResult:
        self._require_lease()
        return self._surface.act(action)

    def act_on_index(self, generation: int, index: int, action: str) -> ActionResult:
        self._require_lease()
        return self._surface.act_on_index(generation, index, action)

    def observe(self) -> Observation:
        return self._surface.observe()

    def resolve(self, locator: Locator) -> Resolution:
        return self._surface.resolve(locator)

    def capture(self) -> EvidenceFrame:
        return self._surface.capture()

    def pending_dialog(self) -> str | None:
        return self._surface.pending_dialog()

    def allowlist_violation(self) -> str | None:
        return self._surface.allowlist_violation()

    def rebind(self, token: str) -> None:
        """E18: updates the token this wrapper checks against, in place -- called by
        `SessionService` (Task 6) when the lease transfers back to the actor this wrapper was
        built for, so this *same object identity* -- the one a long-running caller (a
        session's own replay thread) may already be holding deep inside an in-progress call --
        starts accepting the lease's new current token, with no reconstruction and no
        reference anyone needs to swap out."""
        self._token = token
