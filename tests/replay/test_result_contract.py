import re
from typing import get_args

import pytest
from pydantic import ValidationError

from cua.artifact import models as artifact_models
from cua.artifact.store import save
from cua.replay import result
from cua.replay.result import (
    BusinessOutcome,
    Failure,
    FailureKind,
    Mode,
    Success,
    mint_run_id,
)
from tests.artifact.factories import base


def test_failure_kind_has_exactly_the_thirteen_spec_values() -> None:
    assert set(get_args(FailureKind)) == {
        "INVALID_INPUT", "LOCATOR_NOT_FOUND", "AMBIGUOUS_LOCATOR", "PRECONDITION_FAILED",
        "NO_BRANCH_MATCHED", "OUTPUT_VALIDATION_FAILED", "ALLOWLIST_VIOLATION",
        "UNHANDLED_DIALOG", "POLICY_BLOCKED", "ESCALATION_TIMEOUT", "ESCALATION_UNAVAILABLE",
        "SESSION_LOST", "DURATION_EXCEEDED",
    }


def test_failure_kind_has_exactly_one_implementation() -> None:
    # E4': FailureKind is contract vocabulary declared once in cua.artifact.models; this
    # module re-exports the same object rather than redeclaring the literal.
    assert result.FailureKind is artifact_models.FailureKind


def test_mode_is_closed_to_embedded_and_supervised() -> None:
    assert set(get_args(Mode)) == {"embedded", "supervised"}


def test_a_business_outcome_is_a_plain_value_never_an_exception() -> None:
    assert not issubclass(BusinessOutcome, Exception)
    outcome = BusinessOutcome(
        code="MEMBER_NOT_FOUND", step_id="s3", message="No member found",
        evidence_ref="evidence/run-20260914-153000-ab12",
    )
    assert outcome.code == "MEMBER_NOT_FOUND"


def test_a_failure_requires_non_empty_expected_and_observed() -> None:
    with pytest.raises(ValidationError):
        Failure(kind="LOCATOR_NOT_FOUND", step_id="s2", expected="", observed="not found",
                evidence_ref="evidence/run-x")
    with pytest.raises(ValidationError):
        Failure(kind="LOCATOR_NOT_FOUND", step_id="s2", expected="a unique match", observed="",
                evidence_ref="evidence/run-x")
    ok = Failure(kind="LOCATOR_NOT_FOUND", step_id="s2", expected="a unique match",
                 observed="two matches", evidence_ref="evidence/run-x")
    assert ok.kind == "LOCATOR_NOT_FOUND"


def test_a_success_defaults_to_no_assistance() -> None:
    result_ = Success(outputs={"balance": "4218.60"}, steps_run=["s1", "s2"],
                      evidence_ref="evidence/run-x")
    assert result_.assistance == "none"


def test_mint_run_id_matches_the_documented_shape() -> None:
    run_id = mint_run_id()
    assert re.fullmatch(r"run-\d{14}-[0-9a-f]{4}", run_id), run_id


def test_a_minted_run_id_never_trips_the_criterion_1_scan(tmp_path) -> None:
    run_id = mint_run_id()
    artifact = base()
    artifact.provenance.run_id = run_id
    artifact.provenance.trace_ref = f"evidence/{run_id}/trace.jsonl"
    save(artifact, tmp_path)  # must not raise


# --- Phase 6 / E3, E4: HandbackOutcome, and ResolvedManually's use of the EXISTING
# `assistance` field (no new field -- see E4; these two tests exist only to pin that the
# field this phase relies on already behaves as phase 5 shipped it) ---------------------------

def test_assistance_still_defaults_to_none_and_every_success_construction_stays_valid() -> None:
    result_ = Success(outputs={"balance": "1.00"}, steps_run=["s1"], evidence_ref="evidence/r")
    assert result_.assistance == "none"


def test_a_human_assisted_success_can_be_constructed_via_the_existing_field() -> None:
    result_ = Success(outputs={}, steps_run=["s1"], evidence_ref="evidence/r", assistance="human")
    assert result_.assistance == "human"


def test_handback_outcome_variants_are_distinguishable_by_isinstance() -> None:
    from cua.replay.result import CannotResolve, Resolved, ResolvedManually, RestartFrom

    assert isinstance(Resolved(), Resolved)
    assert isinstance(ResolvedManually(), ResolvedManually)
    assert isinstance(RestartFrom(step_id="s2"), RestartFrom)
    assert isinstance(CannotResolve(note="tried twice, still stuck"), CannotResolve)
