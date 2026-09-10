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
    assert "Sign in" in r.text
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
