"""Spec §7.3, §7.4, §7.7: the intervention record and its pure lifecycle -- create, claim,
expire, hand back. `Clock`-driven throughout (`cua.replay.settle.Clock`, the same protocol
`settle()` takes -- one home, D28), so the whole state machine is provable under `FakeClock`
with no real time anywhere. The live wiring that backs a real wait with a real
`threading.Event` is Task 6's job, on top of this module, not inside it.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Literal

from cua.artifact.models import FailureKind
from cua.replay.result import HandbackOutcome
from cua.replay.settle import Clock

__all__ = ["Intervention", "InterventionExpired", "InterventionStatus", "Interventions"]

InterventionStatus = Literal["open", "claimed", "returned", "expired"]


class InterventionExpired(Exception):
    """Raised by `claim` when the intervention's `ttl_ms` has already elapsed."""


@dataclass
class Intervention:
    """Spec §7.3's fields, verbatim. `handback_outcome` is `None` until `handback()` records
    one; a caller that only needs to render the console reads every other field without it.
    """

    id: str
    run_id: str
    goal: str
    capability_id: str
    version: int
    step_id: str | None
    reason_code: FailureKind
    expected: str
    observed: str
    screenshot_ref: str
    snapshot_ref: str
    allowed_operator_actions: list[str]
    created_at: int
    ttl_ms: int
    claim_ttl_ms: int
    # I3: whether the escalating step's `act()` had already run when this intervention was
    # opened. `True` unless the caller says otherwise -- the common case (a post-act
    # settle timeout, an irreversible step whose action already ran) -- so a `resolved`
    # handback re-verifies rather than re-performs an action, by default. `False` only for
    # a pre-act escalation (the step's own locator/precondition failed before `act()` was
    # ever reached), where a `resolved` handback re-runs the step from scratch instead.
    acted: bool = True
    claimed_by: str | None = None
    claimed_at: int | None = None
    status: InterventionStatus = "open"
    handback_outcome: HandbackOutcome | None = None


@dataclass
class Interventions:
    """One process's live interventions, keyed by id. Not persisted -- the same in-memory,
    process-lifetime scope D34's idempotency-key set already accepted for the same reason:
    a durable store here would duplicate a later phase's job.
    """

    clock: Clock
    _by_id: dict[str, Intervention] = field(default_factory=dict)

    def create(
        self, *, run_id: str, goal: str, capability_id: str, version: int,
        step_id: str | None, reason_code: FailureKind, expected: str, observed: str,
        screenshot_ref: str, snapshot_ref: str, allowed_operator_actions: list[str],
        ttl_ms: int, claim_ttl_ms: int, acted: bool = True,
    ) -> Intervention:
        iv = Intervention(
            id=f"iv-{secrets.token_hex(4)}", run_id=run_id, goal=goal,
            capability_id=capability_id, version=version, step_id=step_id,
            reason_code=reason_code, expected=expected, observed=observed,
            screenshot_ref=screenshot_ref, snapshot_ref=snapshot_ref,
            allowed_operator_actions=allowed_operator_actions,
            created_at=self.clock.monotonic_ms(), ttl_ms=ttl_ms, claim_ttl_ms=claim_ttl_ms,
            acted=acted,
        )
        self._by_id[iv.id] = iv
        return iv

    def get(self, id: str) -> Intervention | None:
        return self._by_id.get(id)

    def list_all(self) -> list[Intervention]:
        """Every intervention this instance has ever created, oldest first (dict insertion
        order) -- the console's own read, so it never reaches into `_by_id` directly."""
        return list(self._by_id.values())

    def claim(self, id: str, operator_id: str, *, clock: Clock) -> Intervention:
        iv = self._by_id[id]
        if iv.status == "open" and clock.monotonic_ms() - iv.created_at >= iv.ttl_ms:
            iv.status = "expired"
            raise InterventionExpired(f"intervention {id!r} expired before it was claimed")
        if iv.status != "open":
            raise ValueError(f"intervention {id!r} is {iv.status!r}, not open")
        iv.status = "claimed"
        iv.claimed_by = operator_id
        iv.claimed_at = clock.monotonic_ms()
        return iv

    def expire_if_due(self, id: str, *, clock: Clock) -> bool:
        """§7.7: an unclaimed intervention expires on `ttl_ms`; a claimed one expires on
        `claim_ttl_ms` from the moment it was claimed, never on the original `ttl_ms` -- a
        claim holds the ttl. Returns whether this call moved it to `expired`; a caller (the
        live session loop) polls this rather than trusting a single timer, since the poll
        itself is what a real `threading.Event.wait(timeout=...)` amounts to.
        """
        iv = self._by_id[id]
        if iv.status == "open" and clock.monotonic_ms() - iv.created_at >= iv.ttl_ms:
            iv.status = "expired"
            return True
        if (iv.status == "claimed" and iv.claimed_at is not None
                and clock.monotonic_ms() - iv.claimed_at >= iv.claim_ttl_ms):
            iv.status = "expired"
            return True
        return False

    def handback(self, id: str, outcome: HandbackOutcome | None = None) -> Intervention:
        iv = self._by_id[id]
        if iv.status != "claimed":
            raise ValueError(f"intervention {id!r} is {iv.status!r}, not claimed")
        iv.status = "returned"
        iv.handback_outcome = outcome
        return iv
