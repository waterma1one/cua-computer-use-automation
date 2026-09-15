from cua.artifact.models import (
    App,
    Artifact,
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
from cua.replay.engine import replay
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
    result = replay(artifact, {"member_id": "12345"}, PoisonSurface(), "embedded")
    assert isinstance(result, Failure) and result.kind == "POLICY_BLOCKED"


def test_a_repeated_idempotency_key_is_refused_on_the_second_call() -> None:
    artifact = _artifact(steps=[
        Step(id="s1", action="click", locator=loc("Post"), risk="irreversible",
             expects=[Expect(when=Matcher(role="heading", name_match="contains", name="Member "),
                             outcome="continue", source="observed")]),
    ])
    surface = FakeSurface(frames=[[node("button", name="Post"),
                                  node("heading", name="Member 12345")]])
    first = replay(artifact, {"member_id": "12345"}, surface, "embedded",
                   confirm_irreversible=True, idempotency_key="k-1", clock=FakeClock())
    assert isinstance(first, ReplaySuccess)
    second = replay(artifact, {"member_id": "12345"}, PoisonSurface(), "embedded",
                    confirm_irreversible=True, idempotency_key="k-1", clock=FakeClock())
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
    deployment = DeploymentAllowlist(allowed_paths=["/"], denied_paths=["/account/close"])
    result = replay(artifact, {"member_id": "12345"}, PoisonSurface(), "embedded",
                    deployment=deployment)
    assert isinstance(result, Failure) and result.kind == "ALLOWLIST_VIOLATION"
