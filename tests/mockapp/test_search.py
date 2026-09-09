from collections.abc import Callable

from fastapi.testclient import TestClient


def test_found_member_reaches_the_detail_screen(client: Callable[..., TestClient]) -> None:
    r = client().post("/search", data={"mid": "12345"})
    assert "Member 12345" in r.text
    assert "4,218.60" in r.text


def test_unknown_member_is_a_business_outcome_not_an_error(
    client: Callable[..., TestClient],
) -> None:
    r = client().post("/search", data={"mid": "00000"})
    assert r.status_code == 200, "a missing member is a normal result, not an HTTP failure"
    assert "No member found" in r.text


def test_restricted_member_is_denied(client: Callable[..., TestClient]) -> None:
    r = client().post("/search", data={"mid": "99999"})
    assert r.status_code == 200
    assert "You are not authorized to view this record" in r.text


def test_malformed_id_is_a_validation_error(client: Callable[..., TestClient]) -> None:
    r = client().post("/search", data={"mid": "abc"})
    assert "Member ID must be five digits" in r.text


def test_detail_screen_has_two_identically_named_row_buttons(
    client: Callable[..., TestClient],
) -> None:
    r = client().get("/member/12345")
    assert r.text.count('value="Select"') == 2, "scope resolution needs an ambiguous name"
