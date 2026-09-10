from collections.abc import Callable

from fastapi.testclient import TestClient

# GET /account/{number} gives the two identically-named "Select" row buttons on
# member.html a real destination, and gives /account/close (the deliberate mutating-GET
# hazard) a link a browser-driving agent can actually reach. Member 12345 carries two
# accounts with different numbers/kinds/balances, so the two rows must render visibly
# different pages -- proving scope resolution rather than merely tolerating it.


def test_account_rows_lead_to_distinguishable_pages(signed_in: Callable[..., TestClient]) -> None:
    c = signed_in()
    savings = c.get("/account/000100045512-01")
    checking = c.get("/account/000100045512-02")
    assert savings.status_code == 200
    assert checking.status_code == 200
    assert "000100045512-01" in savings.text
    assert "000100045512-02" not in savings.text
    assert "4,218.60" in savings.text
    assert "000100045512-02" in checking.text
    assert "000100045512-01" not in checking.text
    assert "312.04" in checking.text


def test_account_detail_page_has_a_close_link(signed_in: Callable[..., TestClient]) -> None:
    r = signed_in().get("/account/000100045512-01")
    assert 'href="/account/close?number=000100045512-01"' in r.text


def test_unknown_account_number_is_not_found_not_a_crash(
    signed_in: Callable[..., TestClient],
) -> None:
    r = signed_in().get("/account/does-not-exist")
    assert r.status_code == 404, "an unknown account is a business outcome, not a server error"
    assert "No member found" in r.text
