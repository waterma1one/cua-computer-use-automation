from dataclasses import dataclass

import pytest

from cua.artifact.models import (
    App,
    Artifact,
    CapabilityPolicy,
    Expect,
    InputSpec,
    Matcher,
    OutputSpec,
    Provenance,
    Recovery,
    Settle,
    Step,
    Success,
    Target,
)
from cua.artifact.validate import DeploymentAllowlist
from cua.policy.allowlist import check_navigation
from cua.replay.engine import Escalator, replay, validate_inputs
from cua.replay.result import (
    BusinessOutcome,
    CannotResolve,
    Failure,
    Resolved,
    ResolvedManually,
    RestartFrom,
)
from cua.replay.result import Success as ReplaySuccess
from cua.surface.base import SurfaceError
from cua.surface.models import EvidenceFrame
from tests.artifact.factories import loc
from tests.replay.conftest import FakeClock, FakeSurface, node


def _artifact(**steps_kwargs) -> Artifact:
    return Artifact(
        schema_version=1, id="corebank.probe", version=1, name="probe",
        description="test capability", verified=False,
        app=App(vendor_product="corebank-teller", variant="base", surface="web",
                entry="/teller/index.html"),
        settle=steps_kwargs.get("settle", Settle(timeout_ms=1000, poll_ms=50)),
        max_duration_ms=60000,
        inputs={"member_id": InputSpec(type="string", pattern="^[0-9]{5}$", required=True)},
        outputs={"balance": OutputSpec(type="string", format="money")},
        steps=steps_kwargs["steps"],
        success=Success(checkpoint=Matcher(role="heading", name_match="contains", name="Member ")),
        provenance=Provenance(discovered_at="2026-09-09T00:00:00", model="gemini-2.5-flash-lite",
                              policy_mode="sandbox", provider_retention="training_permitted",
                              run_id="r_01H", trace_ref="evidence/r_01H/trace.jsonl"),
    )


class PoisonSurface:
    """Every method raises. Proves acceptance criterion 6: invalid input must be refused
    before the surface is touched at all.
    """

    def observe(self): raise AssertionError("a page was created: observe() was called")
    def resolve(self, locator): raise AssertionError("a page was created: resolve() was called")
    def act(self, action): raise AssertionError("a page was created: act() was called")
    def capture(self): raise AssertionError("a page was created: capture() was called")
    def act_on_index(self, generation, index, action):
        raise AssertionError("a page was created: act_on_index() was called")
    def pending_dialog(self):
        raise AssertionError("a page was created: pending_dialog() was called")
    def allowlist_violation(self):
        raise AssertionError("a page was created: allowlist_violation() was called")


class FakeEvidenceSink:
    """Records what the engine sent it, for E16's assertion that a failure captures a frame."""

    def __init__(self) -> None:
        self.run_id = "run-test"
        self.frames: list[tuple[EvidenceFrame, str]] = []

    def event(self, **fields: object) -> None:
        pass

    def frame(self, frame: EvidenceFrame, name: str) -> None:
        self.frames.append((frame, name))

    def evidence_ref(self) -> str:
        return f"evidence/{self.run_id}"


def test_invalid_input_is_refused_before_any_surface_method_is_called() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="fill", locator=loc("Member ID"),
             value={"from_input": "member_id"}, risk="safe"),
    ])
    result = replay(artifact, {"member_id": "not-five-digits"}, PoisonSurface(), "embedded")
    assert isinstance(result, Failure)
    assert result.kind == "INVALID_INPUT"


def test_a_clean_replay_returns_success_with_the_extracted_output() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="fill", locator=loc("Member ID"),
             value={"from_input": "member_id"}, risk="safe"),
        Step(id="s2", action="read", locator=loc("Savings"), extract="value", parse="money",
             into="balance", risk="safe",
             expects=[Expect(when=Matcher(role="heading", name_match="contains", name="Member "),
                             outcome="continue", source="observed")]),
    ])
    surface = FakeSurface(frames=[[
        node("button", name="Member ID"),
        node("button", name="Savings", value="4,218.60"),
        node("heading", name="Member 12345"),
    ]])
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock())
    assert isinstance(result, ReplaySuccess)
    assert result.outputs == {"balance": "4218.60"}
    assert result.steps_run == ["s1", "s2"]


def test_a_business_outcome_step_returns_normally_never_raising() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="click", locator=loc("Search"), risk="safe",
             expects=[Expect(when=Matcher(strategy="text", name_match="contains",
                                         name="No member found"),
                             outcome="business", code="MEMBER_NOT_FOUND", source="observed")]),
    ])
    surface = FakeSurface(frames=[[node("button", name="Search"),
                                  node("text", value="No member found")]])
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock())
    assert isinstance(result, BusinessOutcome)
    assert result.code == "MEMBER_NOT_FOUND"


def test_a_money_parse_failure_is_output_validation_failed_never_a_null() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="read", locator=loc("Savings"), extract="value", parse="money",
             into="balance", risk="safe",
             expects=[Expect(when=Matcher(role="heading", name_match="contains", name="Member "),
                             outcome="continue", source="observed")]),
    ])
    surface = FakeSurface(frames=[[
        node("button", name="Savings", value="not-a-balance"),
        node("heading", name="Member 12345"),
    ]])
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock())
    assert isinstance(result, Failure)
    assert result.kind == "OUTPUT_VALIDATION_FAILED"


def test_timed_out_settle_becomes_no_branch_matched_with_a_non_empty_observed() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="click", locator=loc("Search"), risk="safe",
             expects=[Expect(when=Matcher(role="heading", name="never appears"),
                             outcome="continue", source="observed")]),
    ])
    surface = FakeSurface(frames=[[node("button", name="Search"),
                                  node("text", value="something else entirely")]])
    sink = FakeEvidenceSink()
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock(),
                    evidence=sink)
    assert isinstance(result, Failure)
    assert result.kind == "NO_BRANCH_MATCHED"
    assert result.observed
    # E16: a failure captures a frame for the evidence trail (no pending dialog here).
    assert len(sink.frames) == 1


def test_embedded_mode_turns_an_escalation_trigger_into_escalation_unavailable() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="click", locator=loc("Search"), risk="safe"),
    ])
    artifact.recovery = [Recovery(
        name="session_expired",
        detect=Matcher(strategy="text", name_match="contains", name="Session Expired"),
        handle="escalate",
    )]
    surface = FakeSurface(frames=[[node("button", name="Search"),
                                  node("text", value="Session Expired")]])
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock())
    assert isinstance(result, Failure)
    assert result.kind == "ESCALATION_UNAVAILABLE"


def test_a_matched_fail_clause_returns_its_declared_kind() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="click", locator=loc("Search"), risk="safe",
             expects=[Expect(when=Matcher(strategy="text", name_match="contains",
                                         name="Session Expired"),
                             outcome="fail", code="SESSION_LOST", source="observed")]),
    ])
    surface = FakeSurface(frames=[[node("button", name="Search"),
                                  node("text", value="Session Expired")]])
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "SESSION_LOST"


def test_an_ok_false_action_with_a_pending_dialog_falls_through_to_settle() -> None:
    # E14, direction 1: the dialog must reach the recovery machinery, not be misreported.
    artifact = _artifact(steps=[
        Step(id="s1", action="click", locator=loc("Search"), risk="safe"),
    ])
    surface = FakeSurface(frames=[[node("button", name="Search")]],
                         dialog_messages=["Unexpected dialog"], act_ok=False,
                         act_read_value="a dialog is pending")
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "UNHANDLED_DIALOG"


def test_an_ok_false_action_with_no_dialog_is_precondition_failed() -> None:
    # E14, direction 2.
    artifact = _artifact(steps=[
        Step(id="s1", action="click", locator=loc("Search"), risk="safe"),
    ])
    surface = FakeSurface(frames=[[node("button", name="Search")]], act_ok=False,
                         act_read_value="the control could not be clicked")
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "PRECONDITION_FAILED"


def test_a_locator_matching_nothing_is_locator_not_found() -> None:
    artifact = _artifact(steps=[Step(id="s1", action="click", locator=loc("Search"), risk="safe")])
    surface = FakeSurface(frames=[[node("button", name="Something Else")]])
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "LOCATOR_NOT_FOUND"


def test_two_matching_controls_with_no_ordinal_is_ambiguous_locator() -> None:
    artifact = _artifact(steps=[Step(id="s1", action="click", locator=loc("Search"), risk="safe")])
    surface = FakeSurface(frames=[[node("button", name="Search", index=0),
                                  node("button", name="Search", index=1)]])
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "AMBIGUOUS_LOCATOR"


def test_a_disabled_control_is_precondition_failed() -> None:
    artifact = _artifact(steps=[Step(id="s1", action="click", locator=loc("Search"), risk="safe")])
    surface = FakeSurface(frames=[[node("button", name="Search", disabled=True)]])
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "PRECONDITION_FAILED"


def test_a_zero_max_duration_is_duration_exceeded_before_the_first_step_acts() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="click", locator=loc("Search"), risk="safe"),
    ])
    artifact.max_duration_ms = 0
    result = replay(artifact, {"member_id": "12345"}, PoisonSurface(), "embedded",
                    clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "DURATION_EXCEEDED"


def test_a_raising_surface_yields_session_lost() -> None:
    artifact = _artifact(steps=[Step(id="s1", action="click", locator=loc("Search"), risk="safe")])
    surface = FakeSurface(frames=[[node("button", name="Search")]],
                         raise_on_act=SurfaceError("the page crashed"))
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "SESSION_LOST"


def test_a_checkpoint_that_never_matches_is_no_branch_matched() -> None:
    # E18: success.checkpoint is checked after the last step, not assumed from the last
    # step's own `continue`.
    artifact = _artifact(steps=[
        Step(id="s1", action="click", locator=loc("Search"), risk="safe",
             expects=[Expect(when=Matcher(role="heading", name_match="contains", name="X"),
                             outcome="continue", source="observed")]),
    ])
    surface = FakeSurface(frames=[[node("button", name="Search"), node("heading", name="X")]])
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock())
    assert isinstance(result, Failure)
    assert result.kind == "NO_BRANCH_MATCHED"
    assert "Member" in result.expected


def test_supervised_mode_is_not_yet_implemented() -> None:
    artifact = _artifact(steps=[])
    try:
        replay(artifact, {}, PoisonSurface(), "supervised")
    except NotImplementedError:
        pass
    else:
        raise AssertionError("supervised mode must not silently behave like embedded")


def test_an_irreversible_step_without_confirmation_is_policy_blocked() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="click", locator=loc("Post"), risk="irreversible"),
    ])
    result = replay(artifact, {"member_id": "12345"}, PoisonSurface(), "embedded",
                    status="approved")
    assert isinstance(result, Failure) and result.kind == "POLICY_BLOCKED"
    assert "confirm_irreversible" in result.observed


def _irreversible_artifact(*leading: Step) -> Artifact:
    """A capability whose last step is an irreversible "Post". It binds no output, so it
    declares none -- with E28's unbound-output check, `_artifact`'s default `balance`
    output would otherwise turn every clean run below into OUTPUT_VALIDATION_FAILED.
    """
    artifact = _artifact(steps=[
        *leading,
        Step(id="s1", action="click", locator=loc("Post"), risk="irreversible",
             expects=[Expect(when=Matcher(role="heading", name_match="contains", name="Member "),
                             outcome="continue", source="observed")]),
    ])
    artifact.outputs = {}
    return artifact



def test_a_repeated_idempotency_key_is_refused_on_the_second_call() -> None:
    artifact = _irreversible_artifact()
    surface = FakeSurface(frames=[[node("button", name="Post"),
                                  node("heading", name="Member 12345")]])
    first = replay(artifact, {"member_id": "12345"}, surface, "embedded", status="approved",
                   confirm_irreversible=True, idempotency_key="k-1", clock=FakeClock())
    assert isinstance(first, ReplaySuccess)
    second = replay(artifact, {"member_id": "12345"}, PoisonSurface(), "embedded",
                    status="approved", confirm_irreversible=True, idempotency_key="k-1",
                    clock=FakeClock())
    assert isinstance(second, Failure) and second.kind == "POLICY_BLOCKED"


def test_an_unclassified_step_is_policy_blocked_not_silently_safe() -> None:
    artifact = _artifact(steps=[Step(id="s1", action="click", locator=loc("Search"), risk=None)])
    result = replay(artifact, {"member_id": "12345"}, PoisonSurface(), "embedded")
    assert isinstance(result, Failure) and result.kind == "POLICY_BLOCKED"


def test_a_fail_clause_with_a_bad_code_is_policy_blocked_before_the_surface_is_touched() -> None:
    # E17: an artifact that bypassed load() must not reach the surface with an
    # unconstructible BusinessOutcome/Failure waiting at the end of a poll.
    artifact = _artifact(steps=[
        Step(id="s1", action="click", locator=loc("Search"), risk="safe",
             expects=[Expect(when=Matcher(role="heading", name="x"), outcome="fail",
                             source="observed")]),  # no code
    ])
    result = replay(artifact, {"member_id": "12345"}, PoisonSurface(), "embedded")
    assert isinstance(result, Failure) and result.kind == "POLICY_BLOCKED"


def test_a_business_clause_with_no_code_is_policy_blocked_before_the_surface_is_touched() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="click", locator=loc("Search"), risk="safe",
             expects=[Expect(when=Matcher(role="heading", name="x"), outcome="business",
                             source="observed")]),  # no code
    ])
    result = replay(artifact, {"member_id": "12345"}, PoisonSurface(), "embedded")
    assert isinstance(result, Failure) and result.kind == "POLICY_BLOCKED"


def test_a_navigate_target_denied_by_the_deployment_is_allowlist_violation() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="navigate", target=Target(path="/account/close"), risk="safe"),
    ])
    deployment = DeploymentAllowlist(allowed_paths=["/"], denied_paths=["/account/close"],
                                     allowed_actions=["navigate"])
    result = replay(artifact, {"member_id": "12345"}, PoisonSurface(), "embedded",
                    deployment=deployment)
    assert isinstance(result, Failure) and result.kind == "ALLOWLIST_VIOLATION"
    assert "path" in result.observed


# --- Fix round 1 -------------------------------------------------------------------------


class DeadSurface(FakeSurface):
    """A surface whose every post-resolution method raises: the browser is gone. `resolve`
    still works so a step reaches `act` -- the point is what `_fail` does afterwards.
    """

    def act(self, action):
        raise SurfaceError("the page crashed")

    def pending_dialog(self):
        raise SurfaceError("the page is gone")

    def capture(self):
        raise SurfaceError("the page is gone")


class ResolveRaisesSurface(FakeSurface):
    def resolve(self, locator):
        raise SurfaceError("the frame detached during resolve")


class ObserveRaisesSurface(FakeSurface):
    """`resolve` and `act` work; the first `observe()` -- settle()'s -- raises."""

    def observe(self):
        raise SurfaceError("the context closed during settle")


class RecordingSurface(FakeSurface):
    def __init__(self, frames) -> None:
        super().__init__(frames=frames)
        self.actions: list = []

    def act(self, action):
        self.actions.append(action)
        return super().act(action)


def _member_frame(*extra: object) -> list[list]:
    return [[node("button", name="Post"), node("button", name="Search"),
             node("heading", name="Member 12345"), *extra]]


# Finding 1 / E28: `_fail` is safe on a dead surface.

def test_a_dead_surface_yields_session_lost_and_never_raises_out_of_replay() -> None:
    artifact = _artifact(steps=[Step(id="s1", action="click", locator=loc("Search"), risk="safe")])
    sink = FakeEvidenceSink()
    result = replay(artifact, {"member_id": "12345"}, DeadSurface(frames=_member_frame()),
                    "embedded", clock=FakeClock(), evidence=sink)
    assert isinstance(result, Failure) and result.kind == "SESSION_LOST"
    assert sink.frames == []


# Finding 7 / E28: every SurfaceError, wherever it arises, becomes SESSION_LOST.

def test_a_surface_error_from_resolve_is_session_lost() -> None:
    artifact = _artifact(steps=[Step(id="s1", action="click", locator=loc("Search"), risk="safe")])
    result = replay(artifact, {"member_id": "12345"}, ResolveRaisesSurface(frames=_member_frame()),
                    "embedded", clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "SESSION_LOST"


def test_a_surface_error_from_settle_is_session_lost() -> None:
    artifact = _artifact(steps=[Step(id="s1", action="click", locator=loc("Search"), risk="safe")])
    result = replay(artifact, {"member_id": "12345"}, ObserveRaisesSurface(frames=_member_frame()),
                    "embedded", clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "SESSION_LOST"


# Finding 2 / E27: the idempotency key is required, scoped, and burned at the act.

def test_an_irreversible_step_without_an_idempotency_key_is_policy_blocked() -> None:
    artifact = _irreversible_artifact()
    result = replay(artifact, {"member_id": "12345"}, PoisonSurface(), "embedded",
                    status="approved", confirm_irreversible=True)
    assert isinstance(result, Failure) and result.kind == "POLICY_BLOCKED"
    assert "idempotency_key" in result.expected
    assert "idempotency_key" in result.observed


def test_the_same_key_is_accepted_for_a_different_artifact_id_or_version() -> None:
    first = replay(_irreversible_artifact(), {"member_id": "12345"},
                   FakeSurface(frames=_member_frame()), "embedded", status="approved",
                   confirm_irreversible=True, idempotency_key="k-1", clock=FakeClock())
    assert isinstance(first, ReplaySuccess)

    other_version = _irreversible_artifact()
    other_version.version = 2
    second = replay(other_version, {"member_id": "12345"}, FakeSurface(frames=_member_frame()),
                    "embedded", status="approved", confirm_irreversible=True,
                    idempotency_key="k-1", clock=FakeClock())
    assert isinstance(second, ReplaySuccess)

    other_id = _irreversible_artifact()
    other_id.id = "corebank.other"
    third = replay(other_id, {"member_id": "12345"}, FakeSurface(frames=_member_frame()),
                   "embedded", status="approved", confirm_irreversible=True,
                   idempotency_key="k-1", clock=FakeClock())
    assert isinstance(third, ReplaySuccess)


def test_a_replay_that_fails_before_the_irreversible_act_does_not_burn_the_key() -> None:
    artifact = _irreversible_artifact(
        Step(id="s0", action="click", locator=loc("Search"), risk="safe"),
    )
    # "Search" is absent: s0 fails with LOCATOR_NOT_FOUND before s1 ever acts.
    first = replay(artifact, {"member_id": "12345"},
                   FakeSurface(frames=[[node("button", name="Post")]]), "embedded",
                   status="approved", confirm_irreversible=True, idempotency_key="k-retry",
                   clock=FakeClock())
    assert isinstance(first, Failure) and first.kind == "LOCATOR_NOT_FOUND"

    retry = replay(artifact, {"member_id": "12345"}, FakeSurface(frames=_member_frame()),
                   "embedded", status="approved", confirm_irreversible=True,
                   idempotency_key="k-retry", clock=FakeClock())
    assert isinstance(retry, ReplaySuccess)


def test_a_replay_that_reaches_the_irreversible_act_burns_the_key_even_if_it_fails() -> None:
    artifact = _irreversible_artifact()
    first = replay(artifact, {"member_id": "12345"},
                   FakeSurface(frames=_member_frame(), raise_on_act=SurfaceError("crashed")),
                   "embedded", status="approved", confirm_irreversible=True,
                   idempotency_key="k-burn", clock=FakeClock())
    assert isinstance(first, Failure) and first.kind == "SESSION_LOST"

    retry = replay(artifact, {"member_id": "12345"}, PoisonSurface(), "embedded",
                   status="approved", confirm_irreversible=True, idempotency_key="k-burn",
                   clock=FakeClock())
    assert isinstance(retry, Failure) and retry.kind == "POLICY_BLOCKED"


# Finding 3 / E28: a declared output left unbound is OUTPUT_VALIDATION_FAILED, never `{}`.

def test_a_declared_output_that_no_step_binds_is_output_validation_failed() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="read", into="balance", risk="safe",
             expects=[Expect(when=Matcher(role="heading", name_match="contains", name="Member "),
                             outcome="continue", source="observed")]),
    ])
    sink = FakeEvidenceSink()
    result = replay(artifact, {"member_id": "12345"}, FakeSurface(frames=_member_frame()),
                    "embedded", clock=FakeClock(), evidence=sink)
    assert isinstance(result, Failure)
    assert result.kind == "OUTPUT_VALIDATION_FAILED"
    assert result.step_id is None
    assert "balance" in result.observed
    assert len(sink.frames) == 1


# Finding 4 / E28: `_resolve_step_value` never raises.

@pytest.mark.parametrize("declared_optional", [False, True])
def test_a_from_input_naming_an_absent_input_is_invalid_input_before_the_surface(
    declared_optional: bool,
) -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="fill", locator=loc("Member ID"),
             value={"from_input": "note"}, risk="safe"),
    ])
    if declared_optional:
        artifact.inputs["note"] = InputSpec(type="string", required=False)
    result = replay(artifact, {"member_id": "12345"}, PoisonSurface(), "embedded")
    assert isinstance(result, Failure) and result.kind == "INVALID_INPUT"
    assert result.step_id == "s1"
    assert "note" in result.observed


def test_a_from_step_naming_a_step_that_bound_nothing_is_precondition_failed() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="click", locator=loc("Search"), risk="safe"),
        Step(id="s2", action="fill", locator=loc("Member ID"),
             value={"from_step": "s1"}, risk="safe"),
    ])
    sink = FakeEvidenceSink()
    result = replay(artifact, {"member_id": "12345"},
                    FakeSurface(frames=_member_frame(node("button", name="Member ID"))),
                    "embedded", clock=FakeClock(), evidence=sink)
    assert isinstance(result, Failure) and result.kind == "PRECONDITION_FAILED"
    assert result.step_id == "s2"
    assert len(sink.frames) == 1


def test_a_from_step_is_resolved_by_the_producing_step_id() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="read", locator=loc("Savings"), extract="value", parse="money",
             into="balance", risk="safe"),
        Step(id="s2", action="fill", locator=loc("Member ID"),
             value={"from_step": "s1"}, risk="safe"),
    ])
    surface = RecordingSurface(frames=_member_frame(
        node("button", name="Savings", value="4,218.60"), node("button", name="Member ID"),
    ))
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock())
    assert isinstance(result, ReplaySuccess)
    assert [a.value for a in surface.actions if a.kind == "fill"] == ["4218.60"]


# Minor 6: an INVALID_INPUT from validate_inputs carries the replay's own evidence_ref.

def test_an_invalid_input_failure_carries_the_replays_evidence_ref() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="fill", locator=loc("Member ID"),
             value={"from_input": "member_id"}, risk="safe"),
    ])
    sink = FakeEvidenceSink()
    result = replay(artifact, {"member_id": "bad"}, PoisonSurface(), "embedded", evidence=sink)
    assert isinstance(result, Failure) and result.kind == "INVALID_INPUT"
    assert result.evidence_ref == sink.evidence_ref()


# Minor 8: a matched fail clause's `expected` names what should have happened instead.

def test_a_matched_fail_clause_expected_names_the_continue_clauses() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="click", locator=loc("Search"), risk="safe",
             expects=[
                 Expect(when=Matcher(role="heading", name_match="contains", name="Ready"),
                        outcome="continue", source="observed"),
                 Expect(when=Matcher(strategy="text", name_match="contains",
                                     name="Session Expired"),
                        outcome="fail", code="SESSION_LOST", source="observed"),
             ]),
    ])
    surface = FakeSurface(frames=[[node("button", name="Search"),
                                  node("text", value="Session Expired")]])
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "SESSION_LOST"
    assert "Ready" in result.expected
    assert "Session Expired" not in result.expected
    assert "Session Expired" in result.observed


def test_a_matched_fail_clause_with_no_continue_clause_names_the_checkpoint() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="click", locator=loc("Search"), risk="safe",
             expects=[Expect(when=Matcher(strategy="text", name_match="contains",
                                         name="Session Expired"),
                             outcome="fail", code="SESSION_LOST", source="observed")]),
    ])
    surface = FakeSurface(frames=[[node("button", name="Search"),
                                  node("text", value="Session Expired")]])
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "SESSION_LOST"
    assert "Member " in result.expected
    assert "Session Expired" not in result.expected


# Minor 10: a navigate with no target is PRECONDITION_FAILED, never `Action(value="")`.

def test_a_navigate_with_no_target_is_precondition_failed_before_the_surface() -> None:
    artifact = _artifact(steps=[Step(id="s1", action="navigate", risk="safe")])
    result = replay(artifact, {"member_id": "12345"}, PoisonSurface(), "embedded",
                    clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "PRECONDITION_FAILED"
    assert result.step_id == "s1"


# E29: the engine's navigate gate is `DeploymentAllowlist.permits_path`.

def test_a_navigate_target_outside_every_allowed_prefix_is_allowlist_violation() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="navigate", target=Target(path="/admin"), risk="safe"),
    ])
    deployment = DeploymentAllowlist(allowed_paths=["/teller/"], allowed_actions=["navigate"])
    result = replay(artifact, {"member_id": "12345"}, PoisonSurface(), "embedded",
                    deployment=deployment)
    assert isinstance(result, Failure) and result.kind == "ALLOWLIST_VIOLATION"
    assert "path" in result.observed


# Final fix wave, C1 / E31: a sensitive input's value never enters an INVALID_INPUT Failure.

def test_a_sensitive_inputs_value_never_enters_a_pattern_mismatch_failure() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="fill", locator=loc("Member ID"),
             value={"from_input": "member_id"}, risk="safe"),
    ])
    artifact.inputs["member_id"] = InputSpec(
        type="string", pattern="^[0-9]{5}$", required=True, sensitive=True,
    )
    failure = validate_inputs(artifact, {"member_id": "SECRET1"})
    assert failure is not None and failure.kind == "INVALID_INPUT"
    assert "SECRET1" not in failure.expected
    assert "SECRET1" not in failure.observed
    assert "[REDACTED]" in failure.observed
    # The input's name and the pattern are not the value and may stay.
    assert "member_id" in failure.observed
    assert "^[0-9]{5}$" in failure.expected


def test_a_sensitive_inputs_value_never_enters_a_type_mismatch_failure() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="fill", locator=loc("Member ID"),
             value={"from_input": "member_id"}, risk="safe"),
    ])
    artifact.inputs["member_id"] = InputSpec(type="string", required=True, sensitive=True)
    artifact.inputs["pin"] = InputSpec(type="integer", required=True, sensitive=True)

    string_failure = validate_inputs(artifact, {"member_id": 424242, "pin": 1234})
    assert string_failure is not None and string_failure.kind == "INVALID_INPUT"
    assert "424242" not in string_failure.observed
    assert "[REDACTED]" in string_failure.observed

    integer_failure = validate_inputs(artifact, {"member_id": "12345", "pin": "SECRETPIN"})
    assert integer_failure is not None and integer_failure.kind == "INVALID_INPUT"
    assert "SECRETPIN" not in integer_failure.observed
    assert "[REDACTED]" in integer_failure.observed


def test_a_non_sensitive_inputs_value_still_appears_in_the_failure() -> None:
    # The redaction is driven by the declared flag alone (S4.2 decision 6), so an input
    # not declared sensitive keeps the diagnosable message it always had.
    artifact = _artifact(steps=[
        Step(id="s1", action="fill", locator=loc("Member ID"),
             value={"from_input": "member_id"}, risk="safe"),
    ])
    failure = validate_inputs(artifact, {"member_id": "not-five-digits"})
    assert failure is not None and failure.kind == "INVALID_INPUT"
    assert "not-five-digits" in failure.observed
    assert "[REDACTED]" not in failure.observed


# --- Phase 5: status gate, action gate, violation checks, events -------------------------
# (`Target`, `OutputSpec`, `CapabilityPolicy` join the module's top import block; E402 is
# selected, so no mid-file imports.)


def test_a_risky_step_from_a_draft_is_policy_blocked_before_the_surface() -> None:
    artifact = _artifact(steps=[Step(id="s1", action="click", locator=loc("Transfer History"),
                                     risk="risky")])
    result = replay(artifact, {"member_id": "12345"}, PoisonSurface(), "embedded")
    assert isinstance(result, Failure) and result.kind == "POLICY_BLOCKED"
    assert "requires status 'approved'" in result.observed


def test_a_risky_step_from_an_approved_artifact_runs() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="click", locator=loc("Transfer History"), risk="risky"),
        Step(id="s2", action="read", locator=loc("Savings"), extract="value", parse="money",
             into="balance", risk="safe"),
    ])
    surface = FakeSurface(frames=[[node("button", name="Transfer History"),
                                  node("button", name="Savings", value="4,218.60"),
                                  node("heading", name="Member 12345")]])
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", status="approved",
                    clock=FakeClock())
    assert isinstance(result, ReplaySuccess)


def test_an_action_type_the_deployment_does_not_permit_is_allowlist_violation() -> None:
    artifact = _artifact(steps=[Step(id="s1", action="click", locator=loc("Search"), risk="safe")])
    deployment = DeploymentAllowlist(allowed_paths=["/"], allowed_actions=["navigate", "read"])
    result = replay(artifact, {"member_id": "12345"}, PoisonSurface(), "embedded",
                    deployment=deployment)
    assert isinstance(result, Failure) and result.kind == "ALLOWLIST_VIOLATION"
    assert "action 'click'" in result.observed


def test_the_engine_narrows_by_the_artifacts_own_policy() -> None:
    # E6: intersection, in the engine, whether or not the validator ran.
    artifact = _artifact(steps=[
        Step(id="s1", action="navigate", target=Target(path="/member/12345"), risk="safe"),
    ])
    artifact.policy = CapabilityPolicy(allowed_paths=["/statement/"])
    deployment = DeploymentAllowlist(allowed_paths=["/"], allowed_actions=["navigate"])
    result = replay(artifact, {"member_id": "12345"}, PoisonSurface(), "embedded",
                    deployment=deployment)
    assert isinstance(result, Failure) and result.kind == "ALLOWLIST_VIOLATION"
    assert "/member/12345" in result.observed


def test_a_violation_recorded_by_the_surface_after_act_is_allowlist_violation() -> None:
    # The surface freezes itself (Task 4); the engine reports it ahead of every other
    # reading of what `act` returned -- here `ok=True`, the redirect case.
    class RedirectingSurface(FakeSurface):
        def act(self, action):
            self.violation = "the application navigated to '/member/12345', which is refused"
            return super().act(action)

    artifact = _artifact(steps=[Step(id="s1", action="click", locator=loc("Search"), risk="safe")])
    sink = FakeEvidenceSink()
    surface = RedirectingSurface(frames=[[node("button", name="Search")]])
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock(),
                    evidence=sink)
    assert isinstance(result, Failure) and result.kind == "ALLOWLIST_VIOLATION"
    assert result.step_id == "s1"
    assert "/member/12345" in result.observed
    assert [name for _frame, name in sink.frames] == ["s1"]  # captured: the surface was touched


def test_a_violation_wins_over_session_lost_when_act_raises() -> None:
    # An aborted `goto` raises out of the surface with the violation already recorded.
    class AbortingSurface(FakeSurface):
        def act(self, action):
            self.violation = ("navigation to 'http://x/account/close' refused before the "
                              "request was sent")
            raise SurfaceError("navigate to '/account/close' failed: net::ERR_BLOCKED_BY_CLIENT")

    artifact = _artifact(steps=[
        Step(id="s1", action="navigate", target=Target(path="/account/close"), risk="safe"),
    ])
    result = replay(artifact, {"member_id": "12345"}, AbortingSurface(frames=[[]]), "embedded",
                    clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "ALLOWLIST_VIOLATION"


def test_a_violation_recorded_when_pending_dialog_raises_wins_over_session_lost() -> None:
    # The phase 5 open item: act() returns ok=False, the engine asks the surface about a
    # pending dialog to tell a real precondition failure from one, and *that* call is the
    # one that raises -- with the violation already recorded by the surface. This is the
    # fifth SurfaceError-to-SESSION_LOST site D40 named; it must defer to the recorded
    # violation exactly like the other four.
    class FreezingDialogSurface(FakeSurface):
        def pending_dialog(self):
            self.violation = "the application navigated to '/elsewhere', which is refused"
            raise SurfaceError("the frame detached while checking for a dialog")

    artifact = _artifact(steps=[Step(id="s1", action="click", locator=loc("Search"), risk="safe")])
    surface = FreezingDialogSurface(frames=[[node("button", name="Search")]], act_ok=False)
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "ALLOWLIST_VIOLATION"
    assert result.step_id == "s1"


def test_a_violation_during_settle_is_reported_after_the_step_settles() -> None:
    class LateSurface(FakeSurface):
        def observe(self):
            self.violation = "the application navigated to '/elsewhere', which is refused"
            return super().observe()

    artifact = _artifact(steps=[Step(
        id="s1", action="click", locator=loc("Search"), risk="safe",
        expects=[Expect(when=Matcher(role="heading", name_match="contains", name="Member "),
                        outcome="continue", source="observed")],
    )])
    surface = LateSurface(frames=[[node("button", name="Search"),
                                   node("heading", name="Member 12345")]])
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "ALLOWLIST_VIOLATION"


def test_a_violation_before_the_checkpoint_settles_never_becomes_success() -> None:
    class CheckpointSurface(FakeSurface):
        def observe(self):
            observation = super().observe()
            if self._obs_index >= 2:  # the checkpoint's own observe
                self.violation = "the application navigated to '/elsewhere', which is refused"
            return observation

    artifact = _artifact(steps=[
        Step(id="s2", action="read", locator=loc("Savings"), extract="value", parse="money",
             into="balance", risk="safe"),
    ])
    surface = CheckpointSurface(frames=[[node("button", name="Savings", value="1.00"),
                                         node("heading", name="Member 12345")]])
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "ALLOWLIST_VIOLATION"


def test_a_violation_during_settle_wins_even_when_the_expects_never_match() -> None:
    # Reviewer probe: with the destination's expects authored on the step, a fill whose
    # navigation was aborted used to poll the frozen page to its timeout and report
    # NO_BRANCH_MATCHED. The violation wins, and quickly.
    class LateSurface(FakeSurface):
        def observe(self):
            self.violation = "the application navigated to '/elsewhere', which is refused"
            return super().observe()

    artifact = _artifact(steps=[Step(
        id="s1", action="fill", locator=loc("Member ID"), value={"literal": "12345"}, risk="safe",
        expects=[Expect(when=Matcher(role="heading", name_match="contains", name="Elsewhere"),
                        outcome="continue", source="observed")],
    )])
    artifact.outputs = {}
    clock = FakeClock()
    sink = RecordingSink()
    surface = LateSurface(frames=[[node("button", name="Member ID")]])
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=clock,
                    evidence=sink)
    assert isinstance(result, Failure) and result.kind == "ALLOWLIST_VIOLATION"
    assert len(clock.sleep_calls) <= 1  # settle stopped waiting; it did not poll to the deadline
    # Fix round 2: settle's own Violated outcome reports the violation once -- the
    # post-settle check must not ask again and duplicate the event and the frame.
    violations = [e for e in sink.events if e["kind"] == "allowlist_violation"]
    assert len(violations) == 1
    assert len(sink.frames) == 1


def test_a_fail_clause_matching_the_frozen_page_does_not_hide_the_violation() -> None:
    class LateSurface(FakeSurface):
        def observe(self):
            self.violation = "the application navigated to '/elsewhere', which is refused"
            return super().observe()

    artifact = _artifact(steps=[Step(
        id="s1", action="fill", locator=loc("Member ID"), value={"literal": "12345"}, risk="safe",
        expects=[Expect(when=Matcher(role="button", name="Member ID"), outcome="fail",
                        code="PRECONDITION_FAILED", source="observed")],
    )])
    artifact.outputs = {}
    surface = LateSurface(frames=[[node("button", name="Member ID")]])
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "ALLOWLIST_VIOLATION"


def test_an_irreversible_violation_still_burns_the_idempotency_key() -> None:
    class RedirectingSurface(FakeSurface):
        def act(self, action):
            self.violation = "the application navigated to '/elsewhere', which is refused"
            return super().act(action)

    artifact = _irreversible_artifact()
    surface = RedirectingSurface(frames=_member_frame())
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", status="approved",
                    confirm_irreversible=True, idempotency_key="k-v", clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "ALLOWLIST_VIOLATION"

    # D34: the key is burned before the irreversible act, whatever happens next -- a
    # recorded violation is no different from any other post-act failure in this regard.
    retry = replay(artifact, {"member_id": "12345"}, PoisonSurface(), "embedded",
                   status="approved", confirm_irreversible=True, idempotency_key="k-v",
                   clock=FakeClock())
    assert isinstance(retry, Failure) and retry.kind == "POLICY_BLOCKED"
    assert "already used" in retry.observed


class RecordingSink(FakeEvidenceSink):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict] = []

    def event(self, **fields) -> None:
        self.events.append(dict(fields))


def test_a_bound_event_carries_the_outputs_redact_flag_and_the_value() -> None:
    # E9, engine half: the writer masks on the tag (Task 1 pinned that); the engine's job
    # is to tag correctly and to leave the returned value alone.
    artifact = _artifact(steps=[
        Step(id="s2", action="read", locator=loc("Savings"), extract="value", parse="money",
             into="balance", risk="safe"),
        Step(id="s3", action="read", locator=loc("Kind"), extract="value", into="_kind",
             risk="safe"),
    ])
    artifact.outputs = {"balance": OutputSpec(type="string", format="money", redact=True)}
    sink = RecordingSink()
    surface = FakeSurface(frames=[[node("button", name="Savings", value="4,218.60"),
                                  node("button", name="Kind", value="Savings"),
                                  node("heading", name="Member 12345")]])
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock(),
                    evidence=sink)
    assert isinstance(result, ReplaySuccess) and result.outputs == {"balance": "4218.60"}
    bound = [e for e in sink.events if e["kind"] == "bound"]
    assert bound == [
        {"kind": "bound", "step_id": "s2", "into": "balance", "value": "4218.60", "redact": True},
        {"kind": "bound", "step_id": "s3", "into": "_kind", "value": "Savings", "redact": False},
    ]


def test_no_trace_event_carries_an_actions_value() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="fill", locator=loc("Password"), value={"from_input": "member_id"},
             risk="safe"),
    ])
    artifact.outputs = {}
    # `_artifact` declares member_id with a five-digit pattern; this test feeds a credential
    # through it, so the spec is replaced -- sensitive, no pattern -- or `validate_inputs`
    # refuses pre-loop and the event loop below passes vacuously.
    artifact.inputs = {"member_id": InputSpec(type="string", required=True, sensitive=True)}
    sink = RecordingSink()
    surface = FakeSurface(frames=[[node("button", name="Password"),
                                  node("heading", name="Member 12345")]])
    replay(artifact, {"member_id": "SECRET1"}, surface, "embedded", clock=FakeClock(),
           evidence=sink)
    assert any(e["kind"] == "step_started" and e["step_id"] == "s1" for e in sink.events)
    for event in sink.events:
        assert "SECRET1" not in repr(event), event


def test_an_unhandled_dialog_is_recorded_as_well_as_routed() -> None:
    artifact = _artifact(steps=[Step(
        id="s1", action="click", locator=loc("Search"), risk="safe",
        expects=[Expect(when=Matcher(role="heading", name_match="contains", name="Member "),
                        outcome="continue", source="observed")],
    )])
    sink = RecordingSink()
    surface = FakeSurface(frames=[[node("button", name="Search")]],
                          dialog_messages=[None, "Unexpected dialog"])
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock(),
                    evidence=sink)
    assert isinstance(result, Failure) and result.kind == "UNHANDLED_DIALOG"
    assert {"kind": "dialog", "step_id": "s1", "message": "Unexpected dialog",
            "handling": "unhandled"} in sink.events


# --- Final review fix wave ------------------------------------------------------------------

# Finding 2 / E3: `resolve` is a blocking surface call like `act` and settle, so a
# violation recorded while it ran wins over any of its four failure translations.

def test_a_violation_recorded_during_resolve_wins_over_locator_not_found() -> None:
    class FreezingResolveSurface(FakeSurface):
        def resolve(self, locator):
            # The live case: the page navigated itself somewhere denied while the engine
            # was reaching for a control, so the frozen page resolves nothing.
            self.violation = "the application navigated to '/elsewhere', which is refused"
            return super().resolve(locator)

    artifact = _artifact(steps=[Step(id="s1", action="click", locator=loc("Search"),
                                     risk="safe")])
    sink = FakeEvidenceSink()
    surface = FreezingResolveSurface(frames=[[node("heading", name="Member 12345")]])
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock(),
                    evidence=sink)
    assert isinstance(result, Failure) and result.kind == "ALLOWLIST_VIOLATION"
    assert result.step_id == "s1"
    assert "/elsewhere" in result.observed
    assert [name for _frame, name in sink.frames] == ["s1"]


def test_a_violation_recorded_during_resolve_wins_over_ambiguous_locator() -> None:
    class FreezingResolveSurface(FakeSurface):
        def resolve(self, locator):
            self.violation = "the application navigated to '/elsewhere', which is refused"
            return super().resolve(locator)

    artifact = _artifact(steps=[Step(id="s1", action="click", locator=loc("Search"),
                                     risk="safe")])
    surface = FreezingResolveSurface(frames=[[node("button", name="Search", index=0),
                                              node("button", name="Search", index=1)]])
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "ALLOWLIST_VIOLATION"


def test_a_violation_recorded_when_resolve_raises_wins_over_session_lost() -> None:
    class FreezingResolveSurface(FakeSurface):
        def resolve(self, locator):
            self.violation = "the application navigated to '/elsewhere', which is refused"
            raise SurfaceError("the frame detached during resolve")

    artifact = _artifact(steps=[Step(id="s1", action="click", locator=loc("Search"),
                                     risk="safe")])
    result = replay(artifact, {"member_id": "12345"},
                    FreezingResolveSurface(frames=[[]]), "embedded", clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "ALLOWLIST_VIOLATION"


def test_a_clean_resolve_reports_no_violation_event_of_its_own() -> None:
    # The violation check sits on the failing branches only: `_violation` records an event
    # and a frame, so asking on the success path would double-report what the post-`act`
    # check reports anyway.
    class RedirectingSurface(FakeSurface):
        def act(self, action):
            self.violation = "the application navigated to '/elsewhere', which is refused"
            return super().act(action)

    artifact = _artifact(steps=[Step(id="s1", action="click", locator=loc("Search"),
                                     risk="safe")])
    sink = RecordingSink()
    surface = RedirectingSurface(frames=[[node("button", name="Search")]])
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock(),
                    evidence=sink)
    assert isinstance(result, Failure) and result.kind == "ALLOWLIST_VIOLATION"
    assert len([e for e in sink.events if e["kind"] == "allowlist_violation"]) == 1
    assert len(sink.frames) == 1


# Finding 4: a sensitive input's value can reach a URL's query string through a GET form,
# and from there into the surface's violation reason. The shape-based evidence redaction
# cannot recognise a password, so the engine masks it where the declaration is still known.

def test_a_sensitive_inputs_value_never_reaches_a_violation_failure_or_event() -> None:
    class GetFormSurface(FakeSurface):
        def act(self, action):
            # What `cua.surface.web._display_url` renders for a GET form submission: the
            # query string is kept, so whatever was typed is in the sentence verbatim.
            self.violation = (
                "the application navigated to "
                "'http://127.0.0.1:8000/account/close?password=hunter2', which is refused "
                "(path '/account/close' is denied by prefix '/account/close')"
            )
            return super().act(action)

    artifact = _artifact(steps=[
        Step(id="s1", action="fill", locator=loc("Password"), value={"from_input": "password"},
             risk="safe"),
    ])
    artifact.outputs = {}
    artifact.inputs = {"password": InputSpec(type="string", required=True, sensitive=True)}
    sink = RecordingSink()
    surface = GetFormSurface(frames=[[node("button", name="Password")]])
    result = replay(artifact, {"password": "hunter2"}, surface, "embedded", clock=FakeClock(),
                    evidence=sink)
    assert isinstance(result, Failure) and result.kind == "ALLOWLIST_VIOLATION"
    assert "hunter2" not in result.observed
    assert "[REDACTED]" in result.observed
    # The rest of the sentence survives: the refusal is still diagnosable.
    assert "/account/close" in result.observed
    for event in sink.events:
        assert "hunter2" not in repr(event), event


def test_an_ordinary_inputs_value_is_left_in_a_violation_reason() -> None:
    # Masking is for declared credentials only; a member id in a URL is the diagnosis.
    class RedirectingSurface(FakeSurface):
        def act(self, action):
            self.violation = "the application navigated to '/member/12345', which is refused"
            return super().act(action)

    artifact = _artifact(steps=[Step(id="s1", action="click", locator=loc("Search"),
                                     risk="safe")])
    surface = RedirectingSurface(frames=[[node("button", name="Search")]])
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "ALLOWLIST_VIOLATION"
    assert "/member/12345" in result.observed


# Finding 1, at the seam that carried it: the engine hands `Step.target.path` whole --
# query included -- to `permits_path`.

def test_a_query_string_on_a_navigate_target_cannot_escape_a_deny_prefix() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="navigate", target=Target(path="/account/close?x=/../../foo"),
             risk="safe"),
    ])
    deployment = DeploymentAllowlist(allowed_paths=["/"], denied_paths=["/account/close"],
                                     allowed_actions=["navigate"])
    result = replay(artifact, {"member_id": "12345"}, PoisonSurface(), "embedded",
                    deployment=deployment)
    assert isinstance(result, Failure) and result.kind == "ALLOWLIST_VIOLATION"
    assert "denied by prefix '/account/close'" in result.observed


# Minor 6: one rule, one explanation. The static target gate and `check_navigation` say
# the same sentence about the same refusal.

def test_the_static_navigate_gate_explains_a_refusal_the_way_check_navigation_does() -> None:
    deployment = DeploymentAllowlist(
        allowed_origins=["http://127.0.0.1:8000"], allowed_paths=["/teller/"],
        denied_paths=["/teller/admin/"], allowed_actions=["navigate"],
    )
    for path in ("/teller/admin/close", "/admin"):
        artifact = _artifact(steps=[
            Step(id="s1", action="navigate", target=Target(path=path), risk="safe"),
        ])
        result = replay(artifact, {"member_id": "12345"}, PoisonSurface(), "embedded",
                        deployment=deployment)
        assert isinstance(result, Failure)
        assert result.observed == check_navigation(
            f"http://127.0.0.1:8000{path}", deployment).reason


# --- Phase 6: pluggable escalation ----------------------------------------------------------

class FakeEscalator:
    """Records every call and returns whatever `next(self._outcomes)` yields -- a script of
    handback outcomes, one per escalation, the same shape `FakeSurface.dialog_messages` already
    uses for a sequence of scripted answers.
    """

    def __init__(self, outcomes: list) -> None:
        self._outcomes = iter(outcomes)
        self.calls: list[dict] = []

    def escalate(self, *, step_id, kind, expected, observed):
        self.calls.append(dict(step_id=step_id, kind=kind, expected=expected, observed=observed))
        return next(self._outcomes)


assert isinstance(FakeEscalator([]), Escalator)  # module import time: conformance


def _escalating_recovery() -> Recovery:
    return Recovery(name="popup", detect=Matcher(name_match="contains", name="Unexpected"),
                    handle="escalate")


def test_supervised_with_no_escalator_still_raises_not_implemented() -> None:
    # D31, unchanged: this is the one behaviour that must survive this task untouched.
    artifact = _artifact(steps=[Step(id="s1", action="click", locator=loc("Search"), risk="safe")])
    with pytest.raises(NotImplementedError):
        replay(artifact, {"member_id": "12345"}, PoisonSurface(), "supervised")


def test_a_recovery_escalate_trigger_calls_the_escalator_with_the_recovery_kind() -> None:
    artifact = _artifact(steps=[Step(
        id="s1", action="click", locator=loc("Search"), risk="safe",
        expects=[Expect(when=Matcher(role="heading", name_match="contains", name="Member "),
                        outcome="continue", source="observed")],
    )])
    artifact.recovery = [_escalating_recovery()]
    surface = FakeSurface(frames=[[node("button", name="Search"), node("text", name="Unexpected")]])
    escalator = FakeEscalator([CannotResolve(note="could not tell what happened")])
    result = replay(artifact, {"member_id": "12345"}, surface, "supervised",
                    escalator=escalator, clock=FakeClock())
    assert escalator.calls[0]["step_id"] == "s1"
    assert isinstance(result, Failure)
    assert "could not tell what happened" in result.observed


def test_no_branch_matched_escalates_in_supervised_mode_instead_of_failing_outright() -> None:
    artifact = _artifact(steps=[Step(
        id="s1", action="click", locator=loc("Search"), risk="safe",
        expects=[Expect(when=Matcher(role="heading", name_match="contains", name="Nope"),
                        outcome="continue", source="observed")],
    )])
    surface = FakeSurface(frames=[[node("button", name="Search")]])
    escalator = FakeEscalator([CannotResolve(note="gave up")])
    result = replay(artifact, {"member_id": "12345"}, surface, "supervised",
                    escalator=escalator, clock=FakeClock())
    assert escalator.calls[0]["kind"] == "NO_BRANCH_MATCHED"
    assert isinstance(result, Failure) and result.kind == "NO_BRANCH_MATCHED"
    assert "gave up" in result.observed


def test_an_irreversible_steps_locator_not_found_escalates_as_locator_not_found() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="click", locator=loc("Post"), risk="irreversible"),
    ])
    artifact.outputs = {}
    surface = FakeSurface(frames=[[node("button", name="Something Else")]])
    escalator = FakeEscalator([CannotResolve(note="operator stepped away")])
    result = replay(artifact, {"member_id": "12345"}, surface, "supervised", status="approved",
                    confirm_irreversible=True, idempotency_key="k-2", escalator=escalator,
                    clock=FakeClock())
    assert escalator.calls[0]["kind"] == "LOCATOR_NOT_FOUND"
    assert isinstance(result, Failure) and result.kind == "LOCATOR_NOT_FOUND"
    assert "operator stepped away" in result.observed


def test_an_irreversible_steps_ordinary_success_never_escalates() -> None:
    artifact = _irreversible_artifact()
    surface = FakeSurface(
        frames=[[node("button", name="Post"), node("heading", name="Member 12345")]]
    )
    escalator = FakeEscalator([])  # never called -- next() on it would raise StopIteration
    result = replay(artifact, {"member_id": "12345"}, surface, "supervised", status="approved",
                    confirm_irreversible=True, idempotency_key="k-3", escalator=escalator,
                    clock=FakeClock())
    assert isinstance(result, ReplaySuccess)
    assert escalator.calls == []


# --- Resolved: resume with checkpoint re-verification ----------------------------------------

def test_resolved_resumes_at_the_next_step_after_re_verifying_the_prior_steps_checkpoint() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="click", locator=loc("Search"), risk="safe",
             expects=[Expect(when=Matcher(role="heading", name_match="contains", name="Member "),
                             outcome="continue", source="observed")]),
        Step(id="s2", action="read", locator=loc("Savings"), extract="value", parse="money",
             into="balance", risk="safe",
             expects=[Expect(when=Matcher(role="heading", name_match="contains", name="Never "),
                             outcome="continue", source="observed")]),
    ])
    # s2's own expects can never match (deliberately) -- it will NO_BRANCH_MATCHED and
    # escalate. Per E6, T (=s2) is never re-run on Resolved -- its own act()/extraction
    # already happened during this first attempt, before its settle timed out. Because s2 is
    # the artifact's own last step, it has no continue clause of its own to fall back on, so
    # Resolved's re-verification checkpoint is artifact.success.checkpoint ("Member ") --
    # and since there is no T+1 either, _resume_after goes straight to _run_from's tail,
    # which settles that same success.checkpoint again and ends as Success.
    #
    # Frame accounting (FakeSurface: observe() reads the PRE-increment index then increments;
    # resolve() reads _obs_index - 1, i.e. whatever observe() last returned):
    #   f0 -- s1's resolve (obs_index starts at 0) and s1's settle poll 1 (no match)
    #   f1 -- s1's settle poll 2: matches "Member ", Continue(). obs_index is now 2, so s2's
    #         own resolve (reads _obs_index-1 = 1) reads f1 too -- it MUST already carry the
    #         Savings button (the contract card's original draft put Savings one frame later,
    #         which made s2's own resolve() fail LOCATOR_NOT_FOUND before it ever reached
    #         settle -- the actual bug review finding #6 caught here).
    #   f2 -- held for s2's own settle timeout (NO_BRANCH_MATCHED, escalates) and reused,
    #         once more, for Resolved's own checkpoint re-verification poll -- both still see
    #         "Member 12345", so both pass without needing a fourth, distinct frame.
    savings = node("button", name="Savings", value="4,218.60")
    surface = FakeSurface(frames=[
        [node("button", name="Search")],                       # f0
        [node("heading", name="Member 12345"), savings],        # f1
        [node("heading", name="Member 12345"), savings],        # f2
    ])
    escalator = FakeEscalator([Resolved()])
    result = replay(artifact, {"member_id": "12345"}, surface, "supervised",
                    escalator=escalator, clock=FakeClock())
    assert isinstance(result, ReplaySuccess)
    assert result.outputs == {"balance": "4218.60"}
    assert result.steps_run == ["s1", "s2"]  # s2 recorded by _resume_after once Resolved's
                                              # own re-verification passes -- not re-run


def test_resolved_falls_back_to_checkpoint_when_the_paused_step_is_the_last_one() -> None:
    # Re-traced against the real FakeSurface arithmetic and found already correct as drafted
    # (unlike the test above): s1 is the artifact's own last step (its only step), so it has
    # no continue clause of its own to fall back on and its re-verification checkpoint is
    # artifact.success.checkpoint directly; s1 is also the artifact's only step, so T+1 does
    # not exist either -- _resume_after's single re-verification poll and _run_from's own tail
    # checkpoint settle both read frame index 2 (held, since only 3 frames exist and both
    # polls land past the end), both matching "Member 12345" -- no re-run of s1, straight to
    # Success.
    artifact = _artifact(steps=[
        Step(id="s1", action="click", locator=loc("Search"), risk="safe",
             expects=[Expect(when=Matcher(role="heading", name_match="contains", name="Never "),
                             outcome="continue", source="observed")]),
    ])
    artifact.outputs = {}
    surface = FakeSurface(frames=[
        [node("button", name="Search")],
        [node("heading", name="Member 12345")],  # s1's own settle exhausts -> escalates
        [node("heading", name="Member 12345")],  # Resolved's own re-verification: checkpoint
    ])
    escalator = FakeEscalator([Resolved()])
    result = replay(artifact, {"member_id": "12345"}, surface, "supervised",
                    escalator=escalator, clock=FakeClock())
    assert isinstance(result, ReplaySuccess)


def test_resolved_checkpoint_is_not_the_predecessors_clause_when_the_last_step_escalates() -> None:
    # The discriminating case §7.6 needs: s2 is the artifact's own last step, but it is NOT
    # the first step -- it has a predecessor, s1. s1's own continue clause ("Member ") and
    # artifact.success.checkpoint ("Done") are deliberately two different matchers, and by
    # the time of the handback the on-screen text is "Done", not "Member " -- s1's own text
    # has moved on (realistic: the UI advanced). Per §7.6, s2 has no continue clause of its
    # own (it's the last step), so Resolved's re-verification must fall back straight to
    # success.checkpoint ("Done") and match immediately. The pre-fix code instead checked
    # s1's predecessor clause ("Member "), which this frame does not match -- it would poll
    # to timeout, re-escalate with NO_BRANCH_MATCHED, and call the escalator a second time,
    # for which FakeEscalator has no second outcome scripted: `next()` raises StopIteration.
    #
    # Frame accounting (FakeSurface: observe() reads the PRE-increment index then increments;
    # resolve() reads _obs_index - 1, i.e. whatever observe() last returned):
    #   f0 -- s1's resolve (obs_index starts at 0) and s1's settle poll 1 (no match: "Search"
    #         button only, no heading yet)
    #   f1 -- s1's settle poll 2: matches "Member ", Continue(). obs_index is now 2, so s2's
    #         own resolve (reads _obs_index-1 = 1) reads f1 too -- it MUST already carry s2's
    #         own locator target ("Next").
    #   f2 -- held for s2's own settle timeout (its own "Never " clause never matches;
    #         NO_BRANCH_MATCHED, escalates) and reused, once more, for Resolved's own
    #         checkpoint re-verification poll. f2 carries "Done", not "Member " -- the two
    #         matchers this test exists to pull apart.
    artifact = _artifact(steps=[
        Step(id="s1", action="click", locator=loc("Search"), risk="safe",
             expects=[Expect(when=Matcher(role="heading", name_match="contains", name="Member "),
                             outcome="continue", source="observed")]),
        Step(id="s2", action="click", locator=loc("Next"), risk="safe",
             expects=[Expect(when=Matcher(role="heading", name_match="contains", name="Never "),
                             outcome="continue", source="observed")]),
    ])
    artifact.outputs = {}
    artifact.success = Success(checkpoint=Matcher(role="heading", name_match="contains",
                                                   name="Done"))
    surface = FakeSurface(frames=[
        [node("button", name="Search")],                                # f0
        [node("heading", name="Member 12345"), node("button", name="Next")],  # f1
        [node("heading", name="Done")],                                 # f2
    ])
    escalator = FakeEscalator([Resolved()])  # exactly one outcome -- a second escalate() call
                                              # (the pre-fix code's own failure mode) raises
                                              # StopIteration
    result = replay(artifact, {"member_id": "12345"}, surface, "supervised",
                    escalator=escalator, clock=FakeClock())
    assert isinstance(result, ReplaySuccess)
    assert len(escalator.calls) == 1  # never re-escalated -- the fix matched on the first poll


def test_resolved_escalates_again_when_the_checkpoint_re_verification_fails() -> None:
    # Also re-traced and already correct as drafted: frame index 2 ("Something else
    # entirely", a "text" node, not a heading) is what both s1's own timed-out settle holds
    # on and what Resolved's own re-verification poll reads -- it never matches "Member ", so
    # the re-verification fails and re-escalates with NO_BRANCH_MATCHED, exactly as asserted.
    artifact = _artifact(steps=[
        Step(id="s1", action="click", locator=loc("Search"), risk="safe",
             expects=[Expect(when=Matcher(role="heading", name_match="contains", name="Never "),
                             outcome="continue", source="observed")]),
    ])
    artifact.outputs = {}
    surface = FakeSurface(frames=[
        [node("button", name="Search")],
        [node("heading", name="Member 12345")],
        [node("text", name="Something else entirely")],  # re-verification fails
    ])
    escalator = FakeEscalator([Resolved(), CannotResolve(note="page moved on")])
    result = replay(artifact, {"member_id": "12345"}, surface, "supervised",
                    escalator=escalator, clock=FakeClock())
    assert len(escalator.calls) == 2
    assert escalator.calls[1]["kind"] == "NO_BRANCH_MATCHED"
    assert isinstance(result, Failure)
    assert "page moved on" in result.observed


# --- ResolvedManually ------------------------------------------------------------------------

def test_resolved_manually_ends_the_run_as_an_assisted_success_with_no_outputs() -> None:
    artifact = _artifact(steps=[Step(
        id="s1", action="click", locator=loc("Search"), risk="safe",
        expects=[Expect(when=Matcher(role="heading", name_match="contains", name="Never "),
                        outcome="continue", source="observed")],
    )])
    surface = FakeSurface(
        frames=[[node("button", name="Search")], [node("heading", name="Member 12345")]]
    )
    escalator = FakeEscalator([ResolvedManually()])
    result = replay(artifact, {"member_id": "12345"}, surface, "supervised",
                    escalator=escalator, clock=FakeClock())
    assert isinstance(result, ReplaySuccess)
    assert result.assistance == "human"  # the EXISTING D-104 field (E4) -- no new one
    assert result.outputs == {}


# --- RestartFrom -------------------------------------------------------------------------------

def test_restart_from_resumes_at_the_named_step_with_no_checkpoint_re_verification() -> None:
    # Re-traced and found unsatisfiable as originally drafted, for two independent reasons
    # (review finding #6): (1) s2's own `expects` named a heading ("Never ") that can never
    # appear in any frame by construction, so its SECOND attempt (after RestartFrom) would
    # time out and escalate again too -- but the escalator only has one scripted outcome, so
    # the second escalate() call would raise StopIteration. (2) the asserted `steps_run ==
    # ["s1", "s2", "s1", "s2"]` double-counts s2 -- its first attempt never completes (it
    # escalates), so only 3 entries are ever appended: s1 (1st, succeeds), s1 (2nd, restart,
    # succeeds), s2 (2nd, restart, succeeds).
    #
    # Fixed by giving s2 a matchable-but-not-yet-present target (a "Refreshed" text node that
    # only appears from frame index 5 onward -- simulating "the operator's restart put the
    # page into a state where s2 now settles cleanly") and a short settle timeout (via
    # `_artifact`'s new `settle=` override) so the frame list stays a manageable length: with
    # `Settle(timeout_ms=100, poll_ms=50)`, a timing-out settle takes exactly 3 `observe()`
    # calls (polls at clock 0, 50, 100), not the default spec's 20.
    #
    # Frame-by-frame (obs_index bookkeeping, same FakeSurface arithmetic as the test above):
    #   f0 -- s1(1st)'s resolve + its single Continue() poll (s1 has no `expects`, so it
    #         always settles on its first poll, in 1 observe() call)
    #   f1,f2,f3 -- s2(1st)'s resolve (reads f0) then its 3-poll settle timeout (NO_BRANCH_
    #         MATCHED on f3) -- escalates; RestartFrom(step_id="s1") re-enters _run_from(0)
    #   f4 -- s1(2nd)'s resolve + its single Continue() poll; s2(2nd)'s resolve (reads f4,
    #         Savings still present)
    #   f5 -- s2(2nd)'s own settle: matches "Refreshed" on its very first poll; also what the
    #         artifact's own final checkpoint settle reads afterward (held, still matches
    #         "Member ")
    base = [node("button", name="Search"), node("heading", name="Member 12345"),
            node("button", name="Savings", value="4,218.60")]
    artifact = _artifact(
        settle=Settle(timeout_ms=100, poll_ms=50),
        steps=[
            Step(id="s1", action="click", locator=loc("Search"), risk="safe"),
            Step(id="s2", action="read", locator=loc("Savings"), extract="value", parse="money",
                 into="balance", risk="safe",
                 expects=[Expect(when=Matcher(strategy="text", name_match="contains",
                                             name="Refreshed"),
                                 outcome="continue", source="observed")]),
        ],
    )
    refreshed = [*base, node("text", value="Refreshed")]
    surface = FakeSurface(frames=[base, base, base, base, base, refreshed])
    escalator = FakeEscalator([RestartFrom(step_id="s1")])
    result = replay(artifact, {"member_id": "12345"}, surface, "supervised",
                    escalator=escalator, clock=FakeClock())
    assert isinstance(result, ReplaySuccess)
    assert result.outputs == {"balance": "4218.60"}
    assert result.steps_run == ["s1", "s1", "s2"]  # s2's failed first attempt is never
                                                    # appended; RestartFrom re-runs s1 fully


def test_restart_from_an_unknown_step_id_is_a_precondition_failure() -> None:
    artifact = _artifact(steps=[Step(
        id="s1", action="click", locator=loc("Search"), risk="safe",
        expects=[Expect(when=Matcher(role="heading", name_match="contains", name="Never "),
                        outcome="continue", source="observed")],
    )])
    surface = FakeSurface(
        frames=[[node("button", name="Search")], [node("heading", name="Member 12345")]]
    )
    escalator = FakeEscalator([RestartFrom(step_id="does-not-exist")])
    result = replay(artifact, {"member_id": "12345"}, surface, "supervised",
                    escalator=escalator, clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "PRECONDITION_FAILED"


# --- Fix round 1: D40 after the human's own hand-back (Important #1) --------------------------

class _ViolatingEscalator:
    """Simulates a human's own live drive of the surface tripping the allowlist during the
    escalation window itself -- `escalate()` sets `surface.violation` as a side effect,
    before returning the scripted `HandbackOutcome`, exactly the risk Important #1 flagged:
    nothing else observes this except a fresh `_violation(...)` check made right after
    `escalate()` returns.
    """

    def __init__(self, surface: FakeSurface, reason: str, outcome: object) -> None:
        self._surface = surface
        self._reason = reason
        self._outcome = outcome
        self.calls: list[dict] = []

    def escalate(self, *, step_id, kind, expected, observed):
        self.calls.append(dict(step_id=step_id, kind=kind, expected=expected, observed=observed))
        self._surface.violation = self._reason
        return self._outcome


def test_a_violation_recorded_during_the_escalation_window_outranks_resolved_manually() -> None:
    artifact = _artifact(steps=[Step(
        id="s1", action="click", locator=loc("Search"), risk="safe",
        expects=[Expect(when=Matcher(role="heading", name_match="contains", name="Never "),
                        outcome="continue", source="observed")],
    )])
    surface = FakeSurface(
        frames=[[node("button", name="Search")], [node("heading", name="Member 12345")]]
    )
    escalator = _ViolatingEscalator(
        surface, "the operator navigated off the allowlist", ResolvedManually()
    )
    result = replay(artifact, {"member_id": "12345"}, surface, "supervised",
                    escalator=escalator, clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "ALLOWLIST_VIOLATION"
    assert "the operator navigated off the allowlist" in result.observed


# --- Fix round 1: Resolved on a pre-act failure must genuinely re-run T (Important #2) --------

@dataclass
class _CountingActSurface(FakeSurface):
    """Counts real `act()` calls -- the only way, from outside, to tell whether a step's
    action was genuinely (re-)performed rather than the engine treating a pre-act failure's
    `Resolved` handback as if the action had already happened.
    """

    act_calls: int = 0

    def act(self, action):
        self.act_calls += 1
        return super().act(action)


class _ObservingEscalator:
    """Advances the surface's own observation index as a side effect of `escalate()` --
    simulating the human's own look at the live page during the escalation window -- then
    returns the scripted outcome. The same shape `_ViolatingEscalator` uses above, for a
    different side effect.
    """

    def __init__(self, surface: FakeSurface, observes: int, outcome: object) -> None:
        self._surface = surface
        self._observes = observes
        self._outcome = outcome
        self.calls: list[dict] = []

    def escalate(self, *, step_id, kind, expected, observed):
        self.calls.append(dict(step_id=step_id, kind=kind, expected=expected, observed=observed))
        for _ in range(self._observes):
            self._surface.observe()
        return self._outcome


def test_resolved_on_an_irreversible_steps_pre_act_locator_failure_genuinely_re_runs_it() -> None:
    # s1's first resolve (against frame 0, no "Post" control) fails LOCATOR_NOT_FOUND before
    # `act()` is ever called -- risk="irreversible" escalates regardless of kind. The human's
    # own look at the page (simulated here as two `observe()` calls during `escalate()`,
    # advancing `_obs_index` to 2) puts "Post" in view; `Resolved` must re-run s1 from
    # scratch (fix round 1's `pre_act` routing) rather than treat it as already done --
    # `act_calls == 1` is the only way to see, from outside, that this actually happened:
    # under the pre-fix behaviour `_resume_after` would trivially re-verify the (already-true)
    # checkpoint and mark s1 as run without ever calling `act()`.
    artifact = _irreversible_artifact()
    surface = _CountingActSurface(frames=[
        [node("text", name="not yet")],
        [node("button", name="Post"), node("heading", name="Member 12345")],
    ])
    escalator = _ObservingEscalator(surface, observes=2, outcome=Resolved())
    result = replay(artifact, {"member_id": "12345"}, surface, "supervised", status="approved",
                    confirm_irreversible=True, idempotency_key="k-resolved-pre-act",
                    escalator=escalator, clock=FakeClock())
    assert len(escalator.calls) == 1
    assert escalator.calls[0]["kind"] == "LOCATOR_NOT_FOUND"
    assert escalator.calls[0]["step_id"] == "s1"
    assert isinstance(result, ReplaySuccess)
    assert result.steps_run == ["s1"]
    assert surface.act_calls == 1  # ran exactly once: never on the failed pre-act resolve,
                                    # never twice


# --- Fix round 1: D40 pinned at the new escalation call sites (Important #3) -------------------

def test_an_irreversible_steps_act_recorded_violation_never_calls_the_escalator() -> None:
    # The existing `outcome.kind == "ALLOWLIST_VIOLATION"` exclusion in `_maybe_escalate`
    # should already keep this from ever reaching `escalator.escalate(...)` -- pinning it.
    artifact = _irreversible_artifact()
    surface = FakeSurface(
        frames=[[node("button", name="Post"), node("heading", name="Member 12345")]],
        violation="left the allowlist",
    )
    escalator = FakeEscalator([])  # never called -- next() on it would raise StopIteration
    result = replay(artifact, {"member_id": "12345"}, surface, "supervised", status="approved",
                    confirm_irreversible=True, idempotency_key="k-violation-act",
                    escalator=escalator, clock=FakeClock())
    assert isinstance(result, Failure) and result.kind == "ALLOWLIST_VIOLATION"
    assert escalator.calls == []


@dataclass
class _DelayedViolationSurface(FakeSurface):
    """`allowlist_violation()` reports no violation for the first `trip_after` calls, then a
    sticky one forever after -- simulates the surface freezing partway through a run, at a
    specific, chosen call, rather than from the start (which the D40 checks upstream of
    `_resume_after`'s own checkpoint probe would otherwise catch first).
    """

    trip_after: int = 0
    violation_calls: int = 0

    def allowlist_violation(self):
        self.violation_calls += 1
        if self.violation_calls > self.trip_after:
            return "navigated off the allowlist during the resume checkpoint re-verification"
        return None


def test_resume_afters_own_checkpoint_probe_violation_is_never_escalated() -> None:
    # s1's own settle times out (NO_BRANCH_MATCHED, escalates; `Settle(timeout_ms=100,
    # poll_ms=50)` makes 3 polls). `trip_after=6` was derived empirically against this exact
    # scenario (not hand-derived): calls 1-5 are s1's own post-act/settle/post-settle D40
    # checks, call 6 is `_escalate_and_continue`'s own Important-#1 check right after
    # `escalate()` returns (must still read clean here, or that site would catch it first,
    # not the one under test) -- call 7, the first poll of `_resume_after`'s own
    # `checkpoint_probe` settle, is where this test means the violation to first appear.
    artifact = _artifact(
        settle=Settle(timeout_ms=100, poll_ms=50),
        steps=[
            Step(id="s1", action="click", locator=loc("Search"), risk="safe",
                 expects=[Expect(when=Matcher(role="heading", name_match="contains", name="Never "),
                                 outcome="continue", source="observed")]),
        ],
    )
    artifact.outputs = {}
    surface = _DelayedViolationSurface(trip_after=6, frames=[
        [node("button", name="Search")],
        [node("heading", name="Member 12345")],
        [node("heading", name="Member 12345")],
    ])
    escalator = FakeEscalator([Resolved()])
    result = replay(artifact, {"member_id": "12345"}, surface, "supervised",
                    escalator=escalator, clock=FakeClock())
    assert len(escalator.calls) == 1  # never asked a second time for the same violation
    assert isinstance(result, Failure) and result.kind == "ALLOWLIST_VIOLATION"
