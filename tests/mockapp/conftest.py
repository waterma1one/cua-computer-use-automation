from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from mockapp.app import create_app


@pytest.fixture
def client() -> Callable[..., TestClient]:
    def _make(variant: str = "base") -> TestClient:
        return TestClient(create_app(variant), follow_redirects=True)

    return _make
