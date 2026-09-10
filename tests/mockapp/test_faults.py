import time
from collections.abc import Callable

from fastapi.testclient import TestClient

# Reproducibility is the point: evidence runs must be able to demand a specific failure,
# on demand, on any content route, without waiting for a real-world condition to occur.


def test_session_expiry_can_be_forced(client: Callable[..., TestClient]) -> None:
    r = client().get("/member/12345?fault=expired")
    assert "Session Expired" in r.text


def test_server_error_can_be_forced(client: Callable[..., TestClient]) -> None:
    r = client().get("/member/12345?fault=error_500")
    assert r.status_code == 500
    # A legacy back office does not emit JSON error pages; every other fault returns
    # HTML, and error_500 must match rather than standing out as the one JSON response
    # on the whole surface.
    assert r.headers["content-type"].startswith("text/html")
    assert "<html" in r.text.lower()


def test_slow_load_can_be_forced(client: Callable[..., TestClient], monkeypatch) -> None:
    # The live default (1500ms, MOCKAPP_SLOW_FAULT_MS unset) is what makes this fault
    # observable to a human watching a browser; forcing a small value here keeps the
    # suite fast while still proving the delay actually happens. Lower bound only, per
    # the fix-round ruling, so this cannot flake on a loaded machine.
    monkeypatch.setenv("MOCKAPP_SLOW_FAULT_MS", "50")
    start = time.monotonic()
    r = client().get("/member/12345?fault=slow")
    elapsed = time.monotonic() - start
    assert r.status_code == 200
    assert elapsed >= 0.05, "the slow fault must actually delay the response"


def test_interstitial_notice_can_be_forced(client: Callable[..., TestClient]) -> None:
    r = client().get("/member/12345?fault=notice")
    assert "Scheduled maintenance" in r.text


def test_not_found_fault_can_be_forced(signed_in: Callable[..., TestClient]) -> None:
    # An authenticated request against a real, ordinarily-visible member (12345) --
    # otherwise an unauthenticated probe would prove nothing beyond the login redirect.
    r = signed_in().get("/member/12345?fault=not_found")
    assert r.status_code == 404
    assert "No member found" in r.text


def test_denied_fault_can_be_forced(signed_in: Callable[..., TestClient]) -> None:
    r = signed_in().get("/member/12345?fault=denied")
    assert r.status_code == 403
    assert "You are not authorized to view this record" in r.text


def test_dialog_fault_interrupts_a_real_screen_rather_than_replacing_it(
    signed_in: Callable[..., TestClient],
) -> None:
    # The confirm() must fire over an actual page, not a blank one -- an automation
    # meets a dialog over a member record, which is the condition a recovery rule has
    # to handle. The real browser interrupt itself is verified in a later phase; this
    # only pins that the injected script markup is present alongside the page's normal
    # content.
    r = signed_in().get("/member/12345?fault=dialog")
    assert r.status_code == 200
    assert "Member 12345" in r.text, "the dialog must interrupt a real screen, not replace it"
    assert "window.confirm" in r.text


def test_validation_can_be_forced(client: Callable[..., TestClient]) -> None:
    r = client().get("/member/12345?fault=validation")
    assert r.status_code == 422
    assert "Validation error" in r.text


def test_login_is_required_before_search(client: Callable[..., TestClient]) -> None:
    r = client().get("/search")
    assert "Sign in" in r.text


def test_login_is_required_before_member_detail(client: Callable[..., TestClient]) -> None:
    r = client().get("/member/12345")
    assert "Sign in" in r.text


def test_valid_login_grants_access(client: Callable[..., TestClient]) -> None:
    c = client()
    r = c.post("/login", data={"user": "teller", "password": "teller-demo-pw"})
    assert "Member Search" in r.text
    r = c.get("/member/12345")
    assert "Member 12345" in r.text


def test_invalid_login_is_rejected(client: Callable[..., TestClient]) -> None:
    c = client()
    r = c.post("/login", data={"user": "teller", "password": "wrong"})
    # "Sign in" alone does not pin this: it is the login page's heading and is equally
    # true of the unerrored form. The actual error message is what a later escalation
    # handler matches to tell bad credentials from an expired session, so assert that.
    assert "Invalid username or password" in r.text
    r = c.get("/search")
    assert "Sign in" in r.text, "a rejected login must not grant a session"


def test_unrecognized_fault_name_fails_loudly(client: Callable[..., TestClient]) -> None:
    # A typo (?fault=deny instead of ?fault=denied) must not silently render a normal
    # page -- for a fixture whose job is reproducible evidence, a typo that produces a
    # green result is the worst possible failure mode.
    r = client().get("/member/12345?fault=deny")
    assert r.status_code == 400
    assert "deny" in r.text


def test_another_unrecognized_fault_name_also_fails_loudly(
    client: Callable[..., TestClient],
) -> None:
    r = client().get("/member/12345?fault=notfound")
    assert r.status_code == 400
    assert "notfound" in r.text


def test_absent_fault_param_still_means_no_fault(client: Callable[..., TestClient]) -> None:
    r = client().get("/member/12345")
    assert r.status_code == 200
    assert "Sign in" in r.text, "unauthenticated, not faulted -- the ordinary login redirect"


def test_empty_fault_param_still_means_no_fault(client: Callable[..., TestClient]) -> None:
    r = client().get("/member/12345?fault=")
    assert r.status_code == 200
    assert "Sign in" in r.text


def test_fault_can_be_set_per_app_via_environment_variable(
    client: Callable[..., TestClient], monkeypatch
) -> None:
    monkeypatch.setenv("MOCKAPP_FAULT", "notice")
    r = client().get("/member/12345")
    assert "Scheduled maintenance" in r.text


def test_slow_fault_survives_an_empty_env_var_value(
    client: Callable[..., TestClient], monkeypatch
) -> None:
    # .env.example ships MOCKAPP_SLOW_FAULT_MS empty; `set -a; source .env` therefore
    # exports it as the empty string, not unset. int(os.environ.get(VAR, DEFAULT)) treats
    # that as present and calls int(""), which raises -- and that crash degrades into
    # looking exactly like error_500, so the fault delivered is not the one requested.
    monkeypatch.setenv("MOCKAPP_SLOW_FAULT_MS", "")
    from mockapp import faults

    monkeypatch.setattr(faults, "SLOW_FAULT_MS", 10)
    r = client().get("/member/12345?fault=slow")
    assert r.status_code == 200, "an empty override must fall back to the default, not crash"


def test_session_budget_survives_an_empty_env_var_value(
    client: Callable[..., TestClient], monkeypatch
) -> None:
    monkeypatch.setenv("MOCKAPP_SESSION_MAX_REQUESTS", "")
    c = client()
    r = c.post("/login", data={"user": "teller", "password": "teller-demo-pw"})
    assert "Member Search" in r.text, "an empty override must fall back to the default, not crash"


def test_session_expires_after_the_configured_request_budget(
    client: Callable[..., TestClient], monkeypatch
) -> None:
    monkeypatch.setenv("MOCKAPP_SESSION_MAX_REQUESTS", "2")
    c = client()
    # The auto-followed redirect from POST /login to GET /search already spends one
    # gated request, so with a budget of 2 exactly one more gated request succeeds.
    c.post("/login", data={"user": "teller", "password": "teller-demo-pw"})
    r = c.get("/member/12345")
    assert "Member 12345" in r.text, "the granted budget must still work normally"
    r = c.get("/member/12345")
    assert "Session Expired" in r.text, "the budget must be forceable without waiting in real time"
