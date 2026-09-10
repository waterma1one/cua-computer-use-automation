from collections.abc import Callable

from fastapi.testclient import TestClient

# Variant B differs from the base variant in exactly three ways (see the task brief):
# the search field's accessible name, branding throughout, and an extra "Select branch"
# step inserted into the search flow. /search and /branch are gated behind login like
# every other content route, so every request here goes through `signed_in` rather than
# a bare `client` -- following the same adaptation Task 4 made to its brief's own test
# snippet, which skips login and would otherwise just redirect to /login.


def test_variant_b_renames_the_search_field(signed_in: Callable[..., TestClient]) -> None:
    body = signed_in("b").get("/search").text
    assert 'title="Account Holder ID"' in body
    assert 'title="Member ID"' not in body


def test_variant_b_inserts_a_branch_selection_step(signed_in: Callable[..., TestClient]) -> None:
    r = signed_in("b").post("/search", data={"mid": "12345"})
    assert "Select branch" in r.text


def test_base_variant_has_no_branch_step(signed_in: Callable[..., TestClient]) -> None:
    r = signed_in().post("/search", data={"mid": "12345"})
    assert "Select branch" not in r.text
    assert "Member 12345" in r.text
