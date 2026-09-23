"""The result contract every replay-engine entry point returns.

A replay of one capability against one set of inputs ends in exactly one of three
shapes, and never as a raised exception carrying business meaning:

- `Success` -- the capability's declared success checkpoint matched; `outputs` carries
  whatever the artifact declared it would produce.
- `BusinessOutcome` -- the capability ran to a declared, expected non-success ending
  (a member not found, an account already closed) that is not this engine's failure.
- `Failure` -- the capability did not reach a declared ending at all. `kind` names why,
  drawn from the same closed vocabulary a `fail` expect's `code` names in the artifact
  itself (E4').

`ReplayResult` is a plain union, not a wrapper: the engine constructs and returns one
concrete type directly, so a caller pattern-matches on `isinstance` rather than
unwrapping an envelope.

E4': `FailureKind` is declared once, in `cua.artifact.models`, immediately after
`Outcome` -- an artifact's `fail` expect names a `FailureKind` value at authoring time,
before this module exists as a consumer, so the vocabulary lives with the contract the
artifact is checked against. This module imports it rather than redeclaring it;
`tests/test_architecture.py` pins the identity.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from cua.artifact.models import FailureKind

__all__ = [
    "BusinessOutcome",
    "CannotResolve",
    "Failure",
    "FailureKind",
    "HandbackOutcome",
    "Mode",
    "ReplayResult",
    "Resolved",
    "ResolvedManually",
    "RestartFrom",
    "Success",
    "mint_run_id",
]

# E5: how a replay is driven -- `embedded` runs inline with no human in the loop;
# `supervised` runs with a human available to approve a risky/irreversible step or
# resolve an escalation. Closed, like every other enum-shaped field in this contract.
Mode = Literal["embedded", "supervised"]

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]


class Success(BaseModel):
    """The capability's declared success checkpoint matched."""

    model_config = ConfigDict(extra="forbid")

    outputs: dict[str, object]
    steps_run: list[str]
    evidence_ref: str
    # Defaults to unassisted: most replays never touch an LLM fallback or a human, and a
    # caller that ignores this field still gets a correct "nothing intervened" answer.
    assistance: Literal["none", "llm_fallback", "human"] = "none"


class BusinessOutcome(BaseModel):
    """A declared, expected non-success ending -- a plain value, never an exception.

    Raising this as an exception would make ordinary business logic (member not found,
    account already closed) indistinguishable from a defect in the replay engine itself
    to any caller that only catches exceptions.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    step_id: str
    message: str
    evidence_ref: str


class Failure(BaseModel):
    """The capability did not reach a declared ending.

    `expected`/`observed` are required and non-empty: a failure that cannot say what it
    expected and what it saw instead is not a diagnosable failure.
    """

    model_config = ConfigDict(extra="forbid")

    kind: FailureKind
    step_id: str | None
    expected: NonEmptyStr
    observed: NonEmptyStr
    evidence_ref: str


ReplayResult = Success | BusinessOutcome | Failure


# E3/E6/E4: what a human's resolution of an escalation hands back to the engine, spec
# §7.6. `Resolved`/`ResolvedManually`/`RestartFrom` name how the run should proceed;
# `CannotResolve` ends it as a `Failure` carrying the operator's own note. Plain
# dataclasses, not pydantic models -- this is an in-process handoff between
# `cua.session`-side code and `cua.replay.engine`, never serialised, so there is nothing
# for validation to buy here.
@dataclass
class Resolved:
    """The condition the paused step was waiting on now holds. The engine re-verifies the
    resume checkpoint and continues from the next step (E6) -- the paused step itself is
    never re-run.
    """


@dataclass
class ResolvedManually:
    """The operator completed the capability's goal by hand. The run ends as a
    human-assisted success with no extracted outputs (E4: this reuses `Success`'s
    existing `assistance` field -- there is no new field on `Success` for this).
    """


@dataclass
class RestartFrom:
    """Resume by re-running the artifact from the named step, discarding whatever partial
    progress happened after it.
    """

    step_id: str


@dataclass
class CannotResolve:
    """The operator could not resolve the escalation. The run ends as a `Failure` whose
    `observed` text carries this note.
    """

    note: str


HandbackOutcome = Resolved | ResolvedManually | RestartFrom | CannotResolve


def mint_run_id() -> str:
    """A fresh run id, shaped so it can never trip the artifact's criterion-1 content scan.

    `run-<14-digit UTC timestamp>-<4 hex chars>` contains no `://`, no `//`, no `[@`, no
    `::`, and no TLD-shaped suffix -- the shapes `cua.artifact.validate`'s forbidden-content
    scan (`CRITERION_1_CODES`) looks for. `tests/replay/test_result_contract.py` pins this
    by saving an artifact carrying a minted run id through `cua.artifact.store.save`.
    """
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"run-{timestamp}-{secrets.token_hex(2)}"
