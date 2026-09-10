from collections.abc import Callable

from fastapi.testclient import TestClient

from mockapp import app as app_module

# /subaccount/*, /statement/{id} and /account/close are gated behind login like every
# other content route (Task 3), so every request here goes through the `signed_in`
# fixture rather than a bare client. LEDGER is a module-level list (not app state) so it
# survives across the separate create_app() calls each fixture invocation makes -- that
# is what lets this file prove the flow is genuinely not idempotent.


def test_posting_a_subaccount_is_irreversible_and_recorded(
    signed_in: Callable[..., TestClient],
) -> None:
    app_module.LEDGER.clear()
    c = signed_in()
    review = c.post("/subaccount/review", data={"mid": "12345", "kind": "Savings"})
    assert "Confirm new sub-account" in review.text
    done = c.post("/subaccount/post", data={"mid": "12345", "kind": "Savings"})
    assert "Sub-account opened" in done.text
    assert len(app_module.LEDGER) == 1


def test_posting_twice_creates_two_entries(signed_in: Callable[..., TestClient]) -> None:
    app_module.LEDGER.clear()
    c = signed_in()
    for _ in range(2):
        c.post("/subaccount/post", data={"mid": "12345", "kind": "Savings"})
    assert len(app_module.LEDGER) == 2, (
        "the application is genuinely not idempotent; this is what the runner must protect against"
    )


def test_subaccount_flow_is_denied_for_a_restricted_member(
    signed_in: Callable[..., TestClient],
) -> None:
    c = signed_in()
    r = c.post("/subaccount/review", data={"mid": "99999", "kind": "Savings"})
    assert r.status_code == 403
    assert "You are not authorized to view this record" in r.text


def test_a_fault_can_break_the_irreversible_flow_mid_post(
    signed_in: Callable[..., TestClient],
) -> None:
    app_module.LEDGER.clear()
    c = signed_in()
    r = c.post("/subaccount/post?fault=error_500", data={"mid": "12345", "kind": "Savings"})
    assert r.status_code == 500
    assert len(app_module.LEDGER) == 0, "a fault mid-flow must not leave a partial mutation"


def test_account_close_mutates_on_a_get(signed_in: Callable[..., TestClient]) -> None:
    r = signed_in().get("/account/close?number=000100045512-02")
    assert r.status_code == 200
    assert "Account closed" in r.text


def test_statement_route_renders_for_a_known_member(
    signed_in: Callable[..., TestClient],
) -> None:
    r = signed_in().get("/statement/12345")
    assert r.status_code == 200
    assert "Dana Whitfield" in r.text


def test_member_detail_opens_the_statement_in_a_new_window(
    signed_in: Callable[..., TestClient],
) -> None:
    r = signed_in().get("/member/12345")
    assert 'href="/statement/12345"' in r.text
    assert 'target="_blank"' in r.text
