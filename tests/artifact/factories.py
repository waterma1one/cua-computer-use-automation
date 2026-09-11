"""Shared artifact fixtures for the phase-3 test modules.

These live in their own module, not inside a test module, because Tasks 3 (overlays),
4 (tool-schema export) and 5 (the store) all build on the same minimal artifact. A test
module importing fixtures out of another test module would couple those tasks to this
one's file layout for no reason.

`base()` is deliberately the *smallest artifact that validates clean*: every test
perturbs exactly one thing about it, so a finding a test asserts on can only have come
from the perturbation.
"""

from __future__ import annotations

from typing import Any

from cua.artifact.models import (
    App,
    Artifact,
    InputSpec,
    Matcher,
    OutputSpec,
    Provenance,
    Settle,
    Step,
    Success,
)
from cua.surface.models import Locator, SurfaceSegment

PATH = [SurfaceSegment(kind="window", name="main"), SurfaceSegment(kind="frame", name="content")]


def loc(name: str, ordinal: int | None = None) -> Locator:
    return Locator(
        role="button", name=name, surface_path=PATH, ordinal=ordinal,
        rationale="test fixture", confidence="high",
    )


def base(**over: Any) -> Artifact:
    """A minimal artifact that validates clean, so each test perturbs exactly one thing."""
    fields: dict[str, Any] = dict(
        schema_version=1, id="corebank.probe", version=1, name="probe",
        description="A minimal valid capability.", verified=False,
        app=App(vendor_product="corebank-teller", variant="base", surface="web",
                entry="/teller/index.html"),
        # `settle`, `max_duration_ms`, `success` and `provenance` are required by
        # `Artifact` and carry no bearing on validation; they are here only so the fixture
        # parses at all.
        settle=Settle(timeout_ms=8000, poll_ms=200),
        max_duration_ms=120000,
        success=Success(checkpoint=Matcher(role="heading", name="Member ")),
        provenance=Provenance(
            discovered_at="2026-09-09T00:00:00",
            model="gemini-2.5-flash-lite",
            policy_mode="sandbox",
            provider_retention="training_permitted",
            run_id="r_01H",
            trace_ref="evidence/r_01H/trace.jsonl",
        ),
        inputs={"member_id": InputSpec(type="string", required=True)},
        outputs={"balance": OutputSpec(type="string")},
        steps=[
            Step(id="s1", action="fill", locator=loc("Member ID"),
                 value={"from_input": "member_id"}, risk="safe"),
            Step(id="s2", action="read", locator=loc("Savings"),
                 extract="text", parse="money", into="balance", risk="safe"),
        ],
    )
    fields.update(over)
    return Artifact(**fields)
