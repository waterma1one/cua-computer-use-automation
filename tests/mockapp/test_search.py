from collections.abc import Callable

from fastapi.testclient import TestClient

from mockapp.data import MEMBERS

# /search and /member/{id} are gated behind login (Task 3); every business outcome proved
# here previously is re-proved through an authenticated session via the `signed_in` fixture.


def test_found_member_reaches_the_detail_screen(signed_in: Callable[..., TestClient]) -> None:
    r = signed_in().post("/search", data={"mid": "12345"})
    assert "Member 12345" in r.text
    assert "4,218.60" in r.text


def test_unknown_member_is_a_business_outcome_not_an_error(
    signed_in: Callable[..., TestClient],
) -> None:
    r = signed_in().post("/search", data={"mid": "00000"})
    assert r.status_code == 200, "a missing member is a normal result, not an HTTP failure"
    assert "No member found" in r.text


def test_restricted_member_is_denied(signed_in: Callable[..., TestClient]) -> None:
    r = signed_in().post("/search", data={"mid": "99999"})
    assert r.status_code == 200
    assert "You are not authorized to view this record" in r.text


def test_malformed_id_is_a_validation_error(signed_in: Callable[..., TestClient]) -> None:
    r = signed_in().post("/search", data={"mid": "abc"})
    assert "Member ID must be five digits" in r.text


def test_detail_screen_has_two_identically_named_row_buttons(
    signed_in: Callable[..., TestClient],
) -> None:
    r = signed_in().get("/member/12345")
    assert r.text.count('value="Select"') == 2, "scope resolution needs an ambiguous name"


def test_unknown_member_detail_is_not_found_not_a_crash(
    signed_in: Callable[..., TestClient],
) -> None:
    r = signed_in().get("/member/00000")
    assert r.status_code == 404, "an unknown id is a business outcome, not a server error"
    assert "No member found" in r.text


def test_restricted_member_detail_is_denied_not_rendered(
    signed_in: Callable[..., TestClient],
) -> None:
    restricted = MEMBERS["99999"]
    r = signed_in().get("/member/99999")
    assert r.status_code == 403
    assert "You are not authorized to view this record" in r.text
    assert restricted.name not in r.text
    assert restricted.ssn not in r.text


def test_known_member_detail_is_unchanged(signed_in: Callable[..., TestClient]) -> None:
    r = signed_in().get("/member/12345")
    assert r.status_code == 200
    assert "Member 12345" in r.text
    assert "4,218.60" in r.text
