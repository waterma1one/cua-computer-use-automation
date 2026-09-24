"""The live proof: escalate, claim, take control, hand back, resume -- against the real mock
app, through the real HTTP surface, on real threads. Discharges the phase's acceptance
criteria end to end; the per-task tests already proved each mechanism in isolation.

The human at the keyboard. In `cua serve` the operator drives the headed browser directly;
a test has no hands, and E15 forbids touching a session's `Page` from any thread but the
session's own. So `_Operator` stands in for the human in exactly one place: it replaces the
service's `HumanActionBracket` with a subclass whose `before()` -- the claim's own wake point,
on the session's own thread, after the lease has already transferred to the operator -- runs
the real bracket capture and then drives the real page (a `goto`, a login form). Everything
else is the production path: the real service, the real engine, a real Chromium, and the real
mock app. The page handle comes from a pass-through wrapper around `open_session_page`, so no
private attribute is read.

What this module does not cover: `DELETE /sessions/{id}` (park) has no mid-action abort in
this design -- only "let the current blocking call finish" or "wake a session already waiting
on a human" -- and `test_service.py`'s
`test_delete_parks_a_claimed_session_capturing_before_it_releases_the_lease` already proves
the second case live against this same mock app, so it is not repeated here.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page

import cua.session.service as service_module
from cua.artifact.models import (
    App,
    Artifact,
    Expect,
    InputSpec,
    Matcher,
    Settle,
    Step,
    Success,
    Target,
)
from cua.artifact.store import save
from cua.session.actions import HumanActionBracket
from cua.session.lease import LeaseError
from cua.surface.models import Action, Locator
from cua.surface.web import SessionBrowser, close_session_page, open_session_page
from mockapp.app import DEFAULT_LOGIN_PASSWORD, DEFAULT_LOGIN_USER
from tests.replay.test_engine_integration import (
    MEMBER_CHECKPOINT,
    TOP_LEVEL,
    _login,
    _navigate_only_artifact,
)
from tests.session.conftest import (
    _LOGIN_PAGE,
    AUTH,
    _poll_interventions,
    _poll_result,
    _provenance,
    _save_approved,
    _session_body,
)

_SEARCH_PAGE = Expect(when=Matcher(role="button", name_match="exact", name="Search"),
                      outcome="continue", source="observed", verified=True)
# `/member/12345?fault=not_found` renders the search page with "No member found", signed in
# or not (the fault applies before the login check): never the member line
# `MEMBER_CHECKPOINT` matches.
_MEMBER = Expect(when=MEMBER_CHECKPOINT, outcome="continue", source="observed", verified=True)


def _login_form(role: str, name: str) -> Locator:
    return Locator(role=role, name=name, surface_path=TOP_LEVEL, rationale="login form",
                   confidence="high")


def _login_then_member_not_found(tmp_path) -> Artifact:
    """Signs in through the real login form (so the session holds a real server-side mock-app
    session), then opens a member under the `not_found` fault -- s5 can never settle on the
    member line, so it escalates, with the session still signed in."""
    artifact = Artifact(
        schema_version=1, id="corebank.live_handoff", version=1, name="live_handoff",
        description="sign in, then open a member the fault hides", verified=False,
        app=App(vendor_product="corebank-teller", variant="base", surface="web", entry="/login"),
        settle=Settle(timeout_ms=1500, poll_ms=100), max_duration_ms=60000,
        inputs={"user": InputSpec(type="string", required=True),
                "password": InputSpec(type="string", required=True, sensitive=True)},
        outputs={},
        steps=[
            Step(id="s1", action="navigate", target=Target(path="/login"), risk="safe",
                 expects=[Expect(when=_LOGIN_PAGE, outcome="continue", source="observed",
                                 verified=True)]),
            Step(id="s2", action="fill", locator=_login_form("textbox", "User"),
                 value={"from_input": "user"}, risk="safe"),
            Step(id="s3", action="fill", locator=_login_form("textbox", "Password"),
                 value={"from_input": "password"}, risk="safe"),
            # The Search button renders only on the *authenticated* search page (an anonymous
            # /search redirects to /login): matching it proves the sign-in really happened.
            Step(id="s4", action="click", locator=_login_form("button", "Sign in"), risk="safe",
                 expects=[_SEARCH_PAGE]),
            Step(id="s5", action="navigate", target=Target(path="/member/12345?fault=not_found"),
                 risk="safe", expects=[_MEMBER]),
        ],
        success=Success(checkpoint=MEMBER_CHECKPOINT),
        provenance=_provenance("r_live_handoff"),
    )
    save(artifact, tmp_path)
    return artifact


_CREDENTIALS = {"user": DEFAULT_LOGIN_USER, "password": DEFAULT_LOGIN_PASSWORD}


class _Operator:
    """The human at the keyboard -- see the module docstring. `drive` runs once, on the
    session's own thread, inside the human window a claim opens; `agent_refused` records what
    the automation's own lease-guarded surface did when asked to act in that same window."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, drive: Callable[[Page], None]) -> None:
        self.drive = drive
        self.done = threading.Event()
        self.agent_refused: LeaseError | None = None
        self.error: BaseException | None = None
        self._pages: dict[int, Page] = {}
        operator = self

        def open_and_remember(base_url: str, *, headless: bool = True) -> SessionBrowser:
            session_browser = open_session_page(base_url, headless=headless)
            operator._pages[threading.get_ident()] = session_browser.page
            return session_browser

        class _HumanWindow(HumanActionBracket):
            def before(self, surface: Any, sink: Any) -> None:
                super().before(surface, sink)  # the real bracket capture, first
                try:
                    try:  # the operator holds the lease now: the automation may not act
                        surface.act(Action(kind="navigate", value="/search"))
                    except LeaseError as refused:
                        operator.agent_refused = refused
                    operator.drive(operator._pages[threading.get_ident()])
                except BaseException as exc:  # surfaced by `wait`, never raised into the session
                    operator.error = exc
                finally:
                    operator.done.set()

        monkeypatch.setattr(service_module, "open_session_page", open_and_remember)
        monkeypatch.setattr(service_module, "HumanActionBracket", _HumanWindow)

    def wait(self) -> None:
        assert self.done.wait(timeout=15), "the operator's window never opened"
        if self.error is not None:
            raise AssertionError("the operator's drive failed") from self.error


def _trace(tmp_path, result: dict[str, Any]) -> list[dict[str, Any]]:
    lines = (Path(tmp_path) / result["evidence_ref"] / "trace.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def _steps_started(tmp_path, result: dict[str, Any]) -> list[str]:
    return [e["step_id"] for e in _trace(tmp_path, result) if e.get("kind") == "step_started"]


def test_escalate_claim_resolve_and_resume_to_a_real_success(
    client, tmp_path, live_mockapp, monkeypatch,
) -> None:
    # One step, `_navigate_only_artifact`'s own shape: s1 opens a member under the
    # `not_found` fault (applied before the mock app's login check, so no sign-in is needed
    # to reach it) and can never settle on the member line, so it escalates. The human, once
    # in control, signs in and opens the real member page by hand; `resolved` then
    # re-verifies the resume checkpoint -- for a first step, E6's fallback, the artifact's own
    # `success.checkpoint` -- which the human's navigation now satisfies, and the run ends as
    # an ordinary, non-assisted Success: the *automation's* checkpoint matched, even though a
    # human drove the last hop.
    def sign_in_and_open_the_member_by_hand(page: Page) -> None:
        _login(page)
        page.goto("/member/12345")
        page.wait_for_load_state("networkidle")

    operator = _Operator(monkeypatch, sign_in_and_open_the_member_by_hand)
    artifact = _navigate_only_artifact("/member/12345?fault=not_found", [_MEMBER],
                                       timeout_ms=1500)
    save(artifact, tmp_path)
    created = client.post("/sessions", json=_session_body(tmp_path, live_mockapp, artifact),
                          headers=AUTH)
    assert created.status_code == 201
    sid = created.json()["session_id"]

    [iv] = _poll_interventions(client, sid)
    assert (iv["step_id"], iv["reason_code"], iv["status"]) == ("s1", "NO_BRANCH_MATCHED", "open")
    assert client.get(f"/sessions/{sid}", headers=AUTH).json()["controller"] == "agent"

    claimed = client.post(f"/interventions/{iv['id']}/claim", json={"operator_id": "op-1"},
                          headers=AUTH)
    assert claimed.status_code == 200
    assert client.get(f"/sessions/{sid}", headers=AUTH).json()["controller"] == "operator"
    operator.wait()
    # Criterion 1, live: inside the human window the automation's own surface was refused.
    assert operator.agent_refused is not None
    assert "operator" in str(operator.agent_refused)

    handback = client.post(f"/interventions/{iv['id']}/handback", json={"outcome": "resolved"},
                           headers=AUTH)
    assert handback.status_code == 200
    result = _poll_result(client, sid)

    assert set(result) == {"outputs", "steps_run", "evidence_ref", "assistance"}, result
    assert result["assistance"] == "none"  # E4: the existing field, as Task 4/6 check it
    assert result["steps_run"] == ["s1"]
    [returned] = client.get(f"/sessions/{sid}/interventions", headers=AUTH).json()
    assert returned["status"] == "returned"
    assert returned["claimed_by"] == "op-1"
    assert returned["handback_outcome"] == {"outcome": "resolved"}
    assert client.get(f"/sessions/{sid}", headers=AUTH).json()["controller"] == "none"  # torn down
    run_dir = Path(tmp_path) / result["evidence_ref"]
    for half in ("before", "after"):  # the human window is bracketed on both sides
        assert (run_dir / "screenshots" / f"escalation_1_human_{half}.png").exists()
    snapshots = run_dir / "snapshots"
    assert "No member found" in (snapshots / "escalation_1_human_before.yaml").read_text()
    assert "Member 12345" in (snapshots / "escalation_1_human_after.yaml").read_text()


def test_an_unclaimed_escalation_times_out_and_the_session_tears_down_with_a_logout_attempt(
    client, tmp_path, live_mockapp, monkeypatch,
) -> None:
    # Not `test_service.py`'s always-escalating artifact: it never signs in, so there would be
    # no server-side session for the logout to end and the check below would pass vacuously.
    # This artifact signs in for real (s4 matched the authenticated search page before s5
    # escalated) and the teardown wrapper reads the session's own cookie on the session's own
    # thread, just before the real `close_session_page` runs its logout attempt.
    teardown: dict[str, Any] = {}

    def close_and_remember(session_browser: SessionBrowser, *, logout_path: str | None = None,
                           ) -> None:
        cookies = session_browser.page.context.cookies()
        teardown["cookie"] = next((c["value"] for c in cookies if c["name"] == "session"), None)
        teardown["logout_path"] = logout_path
        close_session_page(session_browser, logout_path=logout_path)

    monkeypatch.setattr(service_module, "close_session_page", close_and_remember)
    artifact = _login_then_member_not_found(tmp_path)
    created = client.post("/sessions", json=_session_body(
        tmp_path, live_mockapp, artifact, inputs=_CREDENTIALS, ttl_ms=300, claim_ttl_ms=1000),
        headers=AUTH)
    sid = created.json()["session_id"]

    result = _poll_result(client, sid)
    assert result["kind"] == "ESCALATION_TIMEOUT"
    [iv] = client.get(f"/sessions/{sid}/interventions", headers=AUTH).json()
    assert (iv["step_id"], iv["status"], iv["claimed_by"]) == ("s5", "expired", None)
    assert client.get(f"/sessions/{sid}", headers=AUTH).json()["controller"] == "none"
    assert teardown["logout_path"] == "/logout"
    assert teardown["cookie"], "the session never held a signed-in mock-app session"

    # The server-side session is gone: a fresh page presenting the same cookie is bounced --
    # the same proof Task 3's close test established, now at the end of a real escalation.
    verifier = open_session_page(live_mockapp)
    try:
        verifier.page.context.add_cookies([{
            "name": "session", "value": teardown["cookie"], "url": live_mockapp,
        }])
        verifier.page.goto("/search")
        assert "/login" in verifier.page.url
    finally:
        close_session_page(verifier)


def test_restart_from_replays_the_named_range_and_reaches_success(
    client, tmp_path, live_mockapp, monkeypatch,
) -> None:
    # s0 is irreversible (a stand-in -- a navigate, marked so the range boundary matters, the
    # same construction test_service.py's restart tests use); s1 is safe; s2 opens a member
    # while signed out, lands on the login page, and escalates. The human signs in, then
    # hands back `restart_from s1`: the automation re-runs exactly [s1, s2] -- never the
    # irreversible s0 -- and s2 now reaches the member page on its own.
    operator = _Operator(monkeypatch, _login)
    login_page = Expect(when=_LOGIN_PAGE, outcome="continue", source="observed", verified=True)
    artifact = _save_approved(tmp_path, Artifact(
        schema_version=1, id="corebank.live_restart", version=1, name="live_restart",
        description="open a member while signed out, to force an escalation", verified=False,
        app=App(vendor_product="corebank-teller", variant="base", surface="web", entry="/login"),
        settle=Settle(timeout_ms=1500, poll_ms=100), max_duration_ms=60000,
        inputs={}, outputs={},
        steps=[
            Step(id="s0", action="navigate", target=Target(path="/login"), risk="irreversible",
                 expects=[login_page]),
            Step(id="s1", action="navigate", target=Target(path="/login"), risk="safe",
                 expects=[login_page]),
            Step(id="s2", action="navigate", target=Target(path="/member/12345"), risk="safe",
                 expects=[_MEMBER]),
        ],
        success=Success(checkpoint=MEMBER_CHECKPOINT),
        provenance=_provenance("r_live_restart"),
    ))
    created = client.post("/sessions", json=_session_body(
        tmp_path, live_mockapp, artifact, confirm_irreversible=True,
        idempotency_key=f"live-restart-{tmp_path.name}"), headers=AUTH)
    assert created.status_code == 201
    sid = created.json()["session_id"]

    [iv] = _poll_interventions(client, sid)
    assert iv["step_id"] == "s2"
    assert "restart_from" in iv["allowed_operator_actions"]
    client.post(f"/interventions/{iv['id']}/claim", json={"operator_id": "op-1"}, headers=AUTH)
    operator.wait()
    handback = client.post(f"/interventions/{iv['id']}/handback",
                           json={"outcome": "restart_from", "step_id": "s1"}, headers=AUTH)
    assert handback.status_code == 200
    result = _poll_result(client, sid)

    assert set(result) == {"outputs", "steps_run", "evidence_ref", "assistance"}, result
    assert result["assistance"] == "none"
    # Only the named range was replayed: s0 started once, s1 and s2 twice each.
    assert _steps_started(tmp_path, result) == ["s0", "s1", "s2", "s1", "s2"]
    [returned] = client.get(f"/sessions/{sid}/interventions", headers=AUTH).json()
    assert returned["handback_outcome"] == {"outcome": "restart_from", "step_id": "s1"}
