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
