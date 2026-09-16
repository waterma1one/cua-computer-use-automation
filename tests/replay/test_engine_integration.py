"""Drives the real phase-1 mock application through every injected fault, through the
real `WebSurface` and the replay engine.

Test-only: this module wires Tasks 1-6 against a live Chromium page (via `browser` and
`live_mockapp` in `tests/conftest.py`), rather than any fake or in-process double.
Discharges acceptance criterion 9.

Live-capture reconciliation (E25): before any `Matcher`/`Locator` below was finalized,
a local live-snapshot capture file was extended with real captures of the
authenticated `/member/12345` frame, the same page with `?fault=expired`, and
`/statement/12345`, plus the `Print statement` link's real accessible name. Every one of
this task's provisional assumptions matched the live capture exactly: the member line and
the "Session Expired" text each render as a single `text` node whose *value* (not name)
carries the string, and the `Print statement` link's accessible name is exactly
"Print statement". No fixture required correction.
"""
import pytest

from cua.artifact.models import (
    App,
    Artifact,
    Expect,
    Matcher,
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
from cua.surface.models import Locator, SurfaceSegment
from cua.surface.web import ObservationBudget, WebSurface
from mockapp.app import DEFAULT_LOGIN_PASSWORD, DEFAULT_LOGIN_USER, LEDGER

# Verified in Task 7 step 1 against the authenticated /member/12345 capture: the member
# line renders as one `text` node whose value is the whole line, not a `heading`-role
# node, so a text-strategy Matcher (which falls back to a node's value when it has no
# name) is what matches it, exact-role locators would not.
MEMBER_CHECKPOINT = Matcher(strategy="text", name_match="prefix", name="Member 12345")

TOP_LEVEL = [SurfaceSegment(kind="window", name="main")]


@pytest.fixture
def surface_page(browser, live_mockapp):
    # E13: the base URL is a property of the page, not of replay() -- every navigate step
    # in this task's artifacts carries a bare path.
    page = browser.new_page(base_url=live_mockapp)
    yield page
    page.close()


def _login(page) -> None:
    page.goto("/login")
    page.get_by_role("textbox", name="User", exact=True).fill(DEFAULT_LOGIN_USER)
    page.get_by_role("textbox", name="Password", exact=True).fill(DEFAULT_LOGIN_PASSWORD)
    page.get_by_role("button", name="Sign in", exact=True).click()
    page.wait_for_load_state("networkidle")


def _navigate_only_artifact(
    path: str,
    expects: list[Expect],
    recovery: list[Recovery] | None = None,
    timeout_ms: int = 3000,
    poll_ms: int = 100,
) -> Artifact:
    return Artifact(
        schema_version=1,
        id="corebank.fault_probe",
        version=1,
        name="fault_probe",
        description="drives one fault route",
        verified=False,
        app=App(vendor_product="corebank-teller", variant="base", surface="web", entry=path),
        settle=Settle(timeout_ms=timeout_ms, poll_ms=poll_ms),
        max_duration_ms=60000,
        inputs={},
        outputs={},
        steps=[
            Step(
                id="s1",
                action="navigate",
                target=Target(path=path),
                risk="safe",
                expects=expects,
            )
        ],
        success=Success(checkpoint=MEMBER_CHECKPOINT),
        recovery=recovery or [],
        provenance=Provenance(
            discovered_at="2026-09-09T00:00:00",
            model="gemini-2.5-flash-lite",
            policy_mode="sandbox",
            provider_retention="training_permitted",
            run_id="r_test",
            trace_ref="evidence/r_test/trace.jsonl",
        ),
    )


def test_validation_fault_yields_a_declared_business_outcome(surface_page) -> None:
    _login(surface_page)
    surface = WebSurface(surface_page, ObservationBudget(max_nodes=200))
    artifact = _navigate_only_artifact(
        "/member/12345?fault=validation",
        [
            Expect(
                when=Matcher(strategy="text", name_match="contains", name="Validation error"),
                outcome="business",
                code="VALIDATION_ERROR",
                source="observed",
            )
        ],
    )
    result = replay(artifact, {}, surface, "embedded")
    assert isinstance(result, BusinessOutcome)
    assert result.code == "VALIDATION_ERROR"


def test_not_found_fault_yields_member_not_found(surface_page) -> None:
    _login(surface_page)
    surface = WebSurface(surface_page, ObservationBudget(max_nodes=200))
    artifact = _navigate_only_artifact(
        "/member/12345?fault=not_found",
        [
            Expect(
                when=Matcher(strategy="text", name_match="contains", name="No member found"),
                outcome="business",
                code="MEMBER_NOT_FOUND",
                source="observed",
            )
        ],
    )
    result = replay(artifact, {}, surface, "embedded")
    assert isinstance(result, BusinessOutcome)
    assert result.code == "MEMBER_NOT_FOUND"


def test_denied_fault_yields_permission_denied(surface_page) -> None:
    _login(surface_page)
    surface = WebSurface(surface_page, ObservationBudget(max_nodes=200))
    artifact = _navigate_only_artifact(
        "/member/12345?fault=denied",
        [
            Expect(
                when=Matcher(strategy="text", name_match="contains", name="not authorized"),
                outcome="business",
                code="PERMISSION_DENIED",
                source="observed",
            )
        ],
    )
    result = replay(artifact, {}, surface, "embedded")
    assert isinstance(result, BusinessOutcome)
    assert result.code == "PERMISSION_DENIED"


def test_dialog_fault_with_no_declared_recovery_is_unhandled_dialog(surface_page) -> None:
    _login(surface_page)
    surface = WebSurface(surface_page, ObservationBudget(max_nodes=200))
    artifact = _navigate_only_artifact(
        "/member/12345?fault=dialog",
        [Expect(when=MEMBER_CHECKPOINT, outcome="continue", source="observed")],
    )
    result = replay(artifact, {}, surface, "embedded")
    assert isinstance(result, Failure)
    assert result.kind == "UNHANDLED_DIALOG"
    assert "Unexpected dialog" in result.observed


def test_session_expiry_in_embedded_mode_is_escalation_unavailable(surface_page) -> None:
    _login(surface_page)
    surface = WebSurface(surface_page, ObservationBudget(max_nodes=200))
    artifact = _navigate_only_artifact(
        "/member/12345?fault=expired",
        [Expect(when=MEMBER_CHECKPOINT, outcome="continue", source="observed")],
        recovery=[
            Recovery(
                name="session_expired",
                detect=Matcher(
                    strategy="text", name_match="contains", name="Session Expired"
                ),
                handle="escalate",
            )
        ],
    )
    result = replay(artifact, {}, surface, "embedded")
    assert isinstance(result, Failure)
    assert result.kind == "ESCALATION_UNAVAILABLE"


def test_a_slow_response_is_tolerated_end_to_end(surface_page, monkeypatch) -> None:
    # E24: this proves end-to-end tolerance of a slow response reaching Success with the
    # right steps_run -- nothing more. It does NOT prove settle()'s own poll loop is what
    # waited: Playwright documents that Locator.click() by default waits for a navigation
    # the click initiates, so the mock app's server-side slow fault is absorbed inside
    # act()'s click handling the same way it already is for navigate, regardless of which
    # action triggers it. The poll loop's own behaviour is proven in Task 3 by FakeClock,
    # in bounded virtual time, with no dependency on Playwright's click-waiting semantics
    # at all.
    monkeypatch.setenv("MOCKAPP_SLOW_FAULT_MS", "300")
    _login(surface_page)
    surface_page.goto("/search")
    surface_page.wait_for_load_state("networkidle")
    surface = WebSurface(surface_page, ObservationBudget(max_nodes=200))
    # /search rendered directly (not through the "/" frameset) is a top-level document
    # with no "content" frame at all -- N5: [window:main], not
    # [window:main, frame:content], or these locators resolve NOT_FOUND regardless of the
    # fault.
    artifact = Artifact(
        schema_version=1,
        id="corebank.slow_probe",
        version=1,
        name="slow_probe",
        description="clicks Search under the slow fault",
        verified=False,
        app=App(vendor_product="corebank-teller", variant="base", surface="web", entry="/search"),
        settle=Settle(timeout_ms=3000, poll_ms=100),
        max_duration_ms=60000,
        inputs={},
        outputs={},
        steps=[
            Step(
                id="s1",
                action="fill",
                locator=Locator(
                    role="textbox",
                    name="Member ID",
                    surface_path=TOP_LEVEL,
                    rationale="the only textbox on the search page",
                    confidence="high",
                ),
                value={"literal": "12345"},
                risk="safe",
            ),
            Step(
                id="s2",
                action="click",
                locator=Locator(
                    role="button",
                    name="Search",
                    surface_path=TOP_LEVEL,
                    rationale="submits the search form",
                    confidence="high",
                ),
                risk="safe",
                expects=[Expect(when=MEMBER_CHECKPOINT, outcome="continue", source="observed")],
            ),
        ],
        success=Success(checkpoint=MEMBER_CHECKPOINT),
        provenance=Provenance(
            discovered_at="2026-09-09T00:00:00",
            model="gemini-2.5-flash-lite",
            policy_mode="sandbox",
            provider_retention="training_permitted",
            run_id="r_slow",
            trace_ref="evidence/r_slow/trace.jsonl",
        ),
    )
    # Only the click's resulting request chain runs after this is set -- the search-form
    # navigation above must not itself pay the slow fault.
    monkeypatch.setenv("MOCKAPP_FAULT", "slow")
    result = replay(artifact, {}, surface, "embedded")
    assert isinstance(result, ReplaySuccess)
    assert result.steps_run == ["s1", "s2"]


def test_allowlist_violation_prevents_the_mutating_get_from_ever_firing(surface_page) -> None:
    _login(surface_page)
    surface = WebSurface(surface_page, ObservationBudget(max_nodes=200))
    artifact = _navigate_only_artifact("/account/close?number=000100045512-01", [])
    before = len(LEDGER)
    deployment = DeploymentAllowlist(allowed_paths=["/"], denied_paths=["/account/close"],
                                     allowed_actions=["navigate"])
    result = replay(artifact, {}, surface, "embedded", deployment=deployment)
    assert isinstance(result, Failure)
    assert result.kind == "ALLOWLIST_VIOLATION"
    assert len(LEDGER) == before, "the deny rule must stop the mutation, not just report it"


def test_a_popup_link_times_out_rather_than_being_silently_followed(surface_page) -> None:
    _login(surface_page)
    surface_page.goto("/member/12345")
    surface_page.wait_for_load_state("networkidle")
    surface = WebSurface(surface_page, ObservationBudget(max_nodes=200))
    # role/name verified in Task 7 step 1: the `<a>` wraps only an
    # `<img title="Print statement">` with no text of its own, so this is what Chromium
    # reports for it, not an assumption.
    popup_locator = Locator(
        role="link",
        name="Print statement",
        surface_path=TOP_LEVEL,  # top-level, not the content frame
        rationale="the print-statement link opens a popup",
        confidence="high",
    )
    artifact = Artifact(
        schema_version=1,
        id="corebank.popup_probe",
        version=1,
        name="popup_probe",
        description="clicks the popup-opening print-statement link",
        verified=False,
        app=App(
            vendor_product="corebank-teller", variant="base", surface="web", entry="/member/12345"
        ),
        settle=Settle(timeout_ms=500, poll_ms=100),
        max_duration_ms=60000,
        inputs={},
        outputs={},
        steps=[
            Step(
                id="s1",
                action="click",
                locator=popup_locator,
                risk="safe",
                # statement.html's real text -- verified in Task 7 step 1 to be a genuine
                # substring of the popup's rendered content: if a later change ever made
                # this surface follow the popup, this clause would truly match it and the
                # test would catch the regression by turning green for the wrong reason
                # instead of staying red.
                expects=[
                    Expect(
                        when=Matcher(
                            strategy="text", name_match="contains", name="Account Statement"
                        ),
                        outcome="continue",
                        source="observed",
                    )
                ],
            )
        ],
        success=Success(checkpoint=MEMBER_CHECKPOINT),
        provenance=Provenance(
            discovered_at="2026-09-09T00:00:00",
            model="gemini-2.5-flash-lite",
            policy_mode="sandbox",
            provider_retention="training_permitted",
            run_id="r_test2",
            trace_ref="evidence/r_test2/trace.jsonl",
        ),
    )
    try:
        result = replay(artifact, {}, surface, "embedded")
        assert isinstance(result, Failure)
        assert result.kind == "NO_BRANCH_MATCHED"
    finally:
        # The click does open a real popup page in surface_page's own context; it is
        # never addressed by this surface (E7), but it must still be closed, not leaked.
        for popup in [p for p in surface_page.context.pages if p is not surface_page]:
            popup.close()
