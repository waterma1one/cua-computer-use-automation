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
from cua.replay.engine import replay, validate_inputs
from cua.replay.result import BusinessOutcome, Failure
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
        settle=Settle(timeout_ms=1000, poll_ms=50), max_duration_ms=60000,
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
    surface = LateSurface(frames=[[node("button", name="Member ID")]])
    result = replay(artifact, {"member_id": "12345"}, surface, "embedded", clock=clock)
    assert isinstance(result, Failure) and result.kind == "ALLOWLIST_VIOLATION"
    assert len(clock.sleep_calls) <= 1  # settle stopped waiting; it did not poll to the deadline


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
