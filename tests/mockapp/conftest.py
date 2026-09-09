from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from mockapp.app import create_app


@pytest.fixture
def client() -> Callable[..., TestClient]:
    def _make(variant: str = "base") -> TestClient:
        return TestClient(create_app(variant), follow_redirects=True)

    return _make


@pytest.fixture
def signed_in(client: Callable[..., TestClient]) -> Callable[..., TestClient]:
    """A `client()`-built TestClient that has already logged in.

    Built on the `client` fixture rather than a parallel client helper, so it inherits
    the same TestClient construction (and its cookie jar carries the session forward
    for every subsequent request on the returned client).
    """

    def _make(variant: str = "base") -> TestClient:
        c = client(variant)
        r = c.post("/login", data={"user": "teller", "password": "teller-demo-pw"})
        assert "Member Search" in r.text, "login failed; the rest of the suite depends on it"
        return c

    return _make
