"""`invoke`: run an approved capability, or turn an irreversible one into a request.

D64: a capability with an irreversible step is never executed here; invoking it creates an
intervention for a human and returns. D65: a catalog-owned ledger refuses a repeated
`(capability, version, idempotency_key)` inside a retention window. D67: anything that is
not approved is refused with `CatalogRefusal`, safe-only drafts included.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from cua.artifact.models import FailureKind
from cua.artifact.validate import DeploymentAllowlist, validate
from cua.catalog.registry import (
    CatalogRefusal,
    entry_for,
    load_gated,
    record_replay,
    resolve_version,
)
from cua.catalog.tools import has_irreversible_step
from cua.replay.engine import EvidenceSink
from cua.replay.engine import replay as run_replay
from cua.replay.result import ReplayResult, mint_run_id
from cua.replay.settle import Clock
from cua.surface.base import Surface

RETENTION_MS = 24 * 60 * 60 * 1000
_REQUEST_TTL_MS = 3_600_000
_REQUEST_CLAIM_TTL_MS = 300_000

SurfaceFactory = Callable[[DeploymentAllowlist], AbstractContextManager[Surface]]


class InterventionSink(Protocol):
    """The slice of `cua.session.interventions.Interventions` the catalog uses, declared
    here so this package does not import the session layer."""

    def create(
        self, *, run_id: str, goal: str, capability_id: str, version: int,
        step_id: str | None, reason_code: FailureKind, expected: str, observed: str,
        screenshot_ref: str, snapshot_ref: str, allowed_operator_actions: list[str],
        ttl_ms: int, claim_ttl_ms: int, acted: bool = True,
    ) -> object: ...


@dataclass
class IdempotencyLedger:
    """In-memory `(capability, version, key)` -> time first seen. Process-local and lost on
    exit (D65). Protects our side only."""

    clock: Clock
    window_ms: int = RETENTION_MS
    _seen: dict[tuple[str, int, str], int] = field(default_factory=dict)

    def claim(self, capability: str, version: int, key: str) -> None:
        now = self.clock.monotonic_ms()
        scope = (capability, version, key)
        first = self._seen.get(scope)
        if first is not None and now - first < self.window_ms:
            raise CatalogRefusal(
                f"idempotency key {key!r} was already used for {capability} v{version} "
                f"inside the {self.window_ms // 3_600_000}h retention window"
            )
        self._seen[scope] = now


@dataclass(frozen=True)
class InterventionRequested:
    """The result of invoking an irreversible capability: a request for a human, not a run."""

    intervention: object


def invoke(
    root: Path, id: str, inputs: dict[str, object], *, deployment: DeploymentAllowlist,
    surface_factory: SurfaceFactory, interventions: InterventionSink,
    ledger: IdempotencyLedger, confirm_irreversible: bool = False,
    idempotency_key: str | None = None, version: int | None = None,
    evidence: EvidenceSink | None = None,
) -> ReplayResult | InterventionRequested:
    """Invokes `id` (latest approved version, else latest draft, unless `version`).

    Raises `CatalogRefusal` for an unknown capability, a draft, an artifact that fails
    validation against `deployment`, or a duplicate idempotency key. Supplying
    `deployment` is what resolves the load-time `ALLOWLIST_NOT_CHECKED` note: policy
    narrowing is checked here, before anything runs.
    """
    resolved = resolve_version(root, id, version)
    artifact, _findings = load_gated(root, id, resolved)
    if entry_for(root, id, resolved).status != "approved":
        raise CatalogRefusal(
            f"{id} v{resolved} is a draft and cannot be invoked; "
            f"approve it first with `cua approve {id} {resolved} --approver <name>`"
        )
    errors = [f for f in validate(artifact, deployment) if f.level == "error"]
    if errors:
        raise CatalogRefusal("; ".join(f"{f.code}: {f.message}" for f in errors))

    if has_irreversible_step(artifact):
        # Burn the key at request time: nothing else in this layer would, since an
        # irreversible invoke never executes (D65).
        if idempotency_key is not None:
            ledger.claim(id, resolved, idempotency_key)
        step_id = next(s.id for s in artifact.steps if s.risk == "irreversible")
        intervention = interventions.create(
            run_id=mint_run_id(), goal=artifact.description, capability_id=id,
            version=resolved, step_id=step_id, reason_code="POLICY_BLOCKED",
            expected="a human approves this irreversible capability before it runs",
            observed="invocation requested; nothing was executed",
            screenshot_ref="", snapshot_ref="",
            allowed_operator_actions=["approve", "deny"],
            ttl_ms=_REQUEST_TTL_MS, claim_ttl_ms=_REQUEST_CLAIM_TTL_MS, acted=False,
        )
        return InterventionRequested(intervention)

    effective = deployment.narrowed_by(artifact.policy)
    with surface_factory(effective) as surface:
        result = run_replay(
            artifact, inputs, surface, "embedded", deployment=effective, status="approved",
            confirm_irreversible=confirm_irreversible, idempotency_key=idempotency_key,
            evidence=evidence,
        )
    record_replay(root, id, resolved, result)
    return result
