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


def test_branding_differs_but_the_search_flow_is_unaffected(
    client: Callable[..., TestClient],
) -> None:
    # / and /nav are not gated behind login, so a bare client is enough here. The point of
    # this difference is that branding must NOT be load-bearing for an automation: the
    # brand text genuinely differs between variants, while the functional link an
    # automation would actually use (Member Search) is identical in both.
    base_body = client().get("/nav").text
    b_body = client("b").get("/nav").text
    assert "Meridian Credit Union" in base_body
    assert "Lakeshore Federal" not in base_body
    assert "Lakeshore Federal" in b_body
    assert "Meridian Credit Union" not in b_body
    assert 'href="/search"' in base_body
    assert 'href="/search"' in b_body


def test_branch_is_not_reachable_under_the_base_variant(
    client: Callable[..., TestClient],
) -> None:
    # /branch exists only to be variant B's inserted step. Under the base variant it must
    # be absent from URL space entirely (a 404), not merely unlinked from the UI -- a route
    # catalog built from registered routes, rather than from observed navigation, would
    # otherwise see a step meant to distinguish B from A silently present in A too.
    r = client().get("/branch?mid=12345")
    assert r.status_code == 404
