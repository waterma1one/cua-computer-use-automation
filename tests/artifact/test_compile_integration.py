"""Spec §8.3 step 7, live: this is the test that proves an artifact whose locator does not
resolve is refused rather than saved -- the project's own central claim, checked directly.

Uses `browser`/`live_mockapp` from `tests/conftest.py`, the same fixtures every other live
integration test in this suite already depends on.
"""
import pytest

from cua.artifact.compile import CompileError, self_verify
from cua.artifact.models import (
    App,
    Artifact,
    InputSpec,
    Matcher,
    Provenance,
    Settle,
    Step,
    Success,
)
from cua.policy.config import PolicyConfig
from cua.surface.models import Locator, SurfaceSegment
from mockapp.app import DEFAULT_LOGIN_PASSWORD, DEFAULT_LOGIN_USER

# A credential is never a literal in an artifact (validate refuses it), so it is an input.
INPUTS = {"password": DEFAULT_LOGIN_PASSWORD}
# The mock app is a frameset at "/": the login form lives in the "content" frame.
TOP_LEVEL = [SurfaceSegment(kind="window", name="main"),
             SurfaceSegment(kind="frame", name="content")]


def _provenance() -> Provenance:
    return Provenance(discovered_at="2026-09-24T00:00:00", model="gemini-3.5-flash-lite",
                      policy_mode="sandbox", provider_retention="training_permitted",
                      run_id="run-20260924000000-abcd",
                      trace_ref="evidence/run-20260924000000-abcd/trace.jsonl")


def _login_steps() -> list[Step]:
    # Discovery would normally record the login itself; a real capability's own steps are
    # exactly what this test hand-builds -- login, land on search, done. Kept minimal on
    # purpose: this test is about self-verification's own gate, not the login flow's shape.
    return [
        Step(id="s1", action="fill", locator=Locator(
            role="textbox", name="User", surface_path=TOP_LEVEL, rationale="t",
            confidence="high"), value={"literal": DEFAULT_LOGIN_USER}, risk="safe"),
        Step(id="s2", action="fill", locator=Locator(
            role="textbox", name="Password", surface_path=TOP_LEVEL, rationale="t",
            confidence="high"), value={"from_input": "password"}, risk="safe"),
        Step(id="s3", action="click", locator=Locator(
            role="button", name="Sign in", surface_path=TOP_LEVEL, rationale="t",
            confidence="high"), risk="safe"),
    ]


def _artifact(**over) -> Artifact:
    fields = dict(
        schema_version=1, id="corebank.login", version=1, name="login", description="d",
        verified=False,
        app=App(vendor_product="corebank-teller", variant="base", surface="web",
                entry="/"),
        settle=Settle(timeout_ms=3000, poll_ms=100), max_duration_ms=30000,
        inputs={"password": InputSpec(type="string", sensitive=True)},
        outputs={}, steps=_login_steps(),
        # D10: the mock app carries no ARIA roles at all -- "Member Search" is
        # `<font size="4"><b>Member Search</b></font>` inside a `<td>`, not a heading.
        # Outcome checkpoints match as text, never by role, per D10's own precedent.
        success=Success(checkpoint=Matcher(
            strategy="text", role=None, name="Member Search", name_match="contains")),
        provenance=_provenance(),
    )
    fields.update(over)
    return Artifact(**fields)


def test_self_verify_marks_a_working_artifact_verified(live_mockapp) -> None:
    artifact = _artifact()
    verified = self_verify(artifact, live_mockapp, inputs=INPUTS)
    assert verified.verified is True
    assert artifact.verified is False  # the input artifact is never mutated in place


def test_self_verify_refuses_an_artifact_whose_locator_does_not_resolve(live_mockapp) -> None:
    # This is the test protecting the project's central claim: a locator that cannot
    # possibly resolve (a control that does not exist) must refuse, never silently save.
    broken_steps = _login_steps()
    broken_steps[0] = Step(
        id="s1", action="fill",
        locator=Locator(role="textbox", name="Definitely Not A Real Field",
                        surface_path=TOP_LEVEL, rationale="t", confidence="high"),
        value={"literal": DEFAULT_LOGIN_USER}, risk="safe",
    )
    artifact = _artifact(steps=broken_steps)
    with pytest.raises(CompileError, match="self-verification"):
        self_verify(artifact, live_mockapp, inputs=INPUTS)


def test_self_verify_uses_a_fresh_session_never_the_discovery_pages_own_state(
    live_mockapp,
) -> None:
    # A page already logged in (as a live discovery session would be, by the time the model
    # calls finish) must not be what self-verification replays against -- self_verify opens
    # its OWN page every time, proven here by calling it twice in a row and getting a real
    # login both times rather than the second one failing on an already-authenticated page's
    # different control layout.
    artifact = _artifact()
    first = self_verify(artifact, live_mockapp, inputs=INPUTS)
    second = self_verify(artifact, live_mockapp, inputs=INPUTS)
    assert first.verified is True
    assert second.verified is True


def test_self_verify_enforces_the_supplied_policy(live_mockapp) -> None:
    # A policy that permits some other origin only must refuse the replay's navigation
    # (D43): self-verify never falls back to a permissive engine default.
    policy = PolicyConfig(allowed_origins=["http://127.0.0.1:1"], allowed_paths=["/"],
                          allowed_actions=["click", "fill", "navigate"])
    with pytest.raises(CompileError, match="self-verification"):
        self_verify(_artifact(), live_mockapp, inputs=INPUTS, policy=policy)


def test_self_verify_refuses_a_risky_step_in_a_draft_artifact(live_mockapp) -> None:
    # A candidate has no registry entry, so it replays as a draft: a risky step needs
    # approval, which self-verification has no business granting itself.
    steps = _login_steps()
    steps[2] = steps[2].model_copy(update={"risk": "risky"})
    with pytest.raises(CompileError, match="self-verification"):
        self_verify(_artifact(steps=steps), live_mockapp, inputs=INPUTS)


def test_self_verify_writes_evidence_through_the_supplied_sink(live_mockapp, tmp_path) -> None:
    from cua.observability.evidence import EvidenceWriter

    writer = EvidenceWriter(tmp_path)
    self_verify(_artifact(), live_mockapp, inputs=INPUTS, evidence=writer)
    assert any(tmp_path.rglob("*.jsonl"))


def _origin_refusing_policy() -> PolicyConfig:
    # Passes every pre-check (validate and `permits_path` look at paths and actions only)
    # but the navigation guard refuses the entry URL's origin at runtime.
    return PolicyConfig(allowed_origins=["http://127.0.0.1:1"], allowed_paths=["/"],
                        allowed_actions=["click", "fill", "navigate"])


def test_self_verify_reports_a_guard_refused_entry_as_an_allowlist_violation(
    live_mockapp,
) -> None:
    # D40: a recorded violation outranks the generic "could not open entry" reading.
    with pytest.raises(CompileError) as caught:
        self_verify(_artifact(), live_mockapp, inputs=INPUTS, policy=_origin_refusing_policy())
    message = str(caught.value)
    assert "allowlist violation" in message
    assert "is not an allowed origin" in message
    assert "could not open entry" not in message


def test_self_verify_records_an_entry_violation_in_the_evidence_sink(
    live_mockapp, tmp_path,
) -> None:
    from cua.observability.evidence import EvidenceWriter

    writer = EvidenceWriter(tmp_path)
    with pytest.raises(CompileError):
        self_verify(_artifact(), live_mockapp, inputs=INPUTS,
                    policy=_origin_refusing_policy(), evidence=writer)
    events = "".join(p.read_text() for p in tmp_path.rglob("*.jsonl"))
    assert '"allowlist_violation"' in events
