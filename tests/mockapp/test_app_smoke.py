from collections.abc import Callable

from fastapi.testclient import TestClient


def test_root_is_a_classic_frameset(client: Callable[..., TestClient]) -> None:
    body = client().get("/").text.lower()
    assert "<frameset" in body, "the surface must be a real frameset, not iframes"
    assert 'name="content"' in body
    assert 'name="nav"' in body


def test_frameset_names_are_stable_addresses(client: Callable[..., TestClient]) -> None:
    body = client().get("/").text
    # The surface layer addresses frames by name, so these are part of the contract.
    assert 'name="content"' in body and 'name="nav"' in body


# The two assertions below pin deliberate hostile markup, not defects. Nothing else in
# the suite references either control, and both are exactly what a future edit would
# "helpfully" clean up -- the point of these tests is that such a cleanup fails the suite.


def test_search_note_input_has_no_accessible_name(signed_in: Callable[..., TestClient]) -> None:
    r = signed_in().get("/search")
    # No id, no <label>, no title -- genuinely no accessible name, on purpose.
    assert '<input type="text" name="note" size="20">' in r.text
    assert "<label" not in r.text
    assert 'id="note"' not in r.text


def test_print_statement_icon_has_a_title_but_no_alt(
    signed_in: Callable[..., TestClient],
) -> None:
    r = signed_in().get("/member/12345")
    # Its accessible name comes only from the title attribute; there is no alt at all.
    assert 'title="Print statement"' in r.text
    assert "alt=" not in r.text
