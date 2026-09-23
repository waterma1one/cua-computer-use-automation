"""Acceptance criterion 8: the console requires the shared token from CUA_OPERATOR_TOKEN.

No live session or browser is needed for any test here: `GET /console` and
`GET /console/interventions/{id}` only ever read `self._sessions` (empty in every test
below) and render a template -- the live escalate/claim/handback cycle these pages point at
is proven end to end against a real mock app in `tests/session/test_service.py` and
`tests/session/test_handoff_integration.py`.
"""
from fastapi.testclient import TestClient

from cua.session.service import create_app

TOKEN = "test-operator-token"


def test_console_without_a_token_is_refused() -> None:
    client = TestClient(create_app(operator_token=TOKEN))
    response = client.get("/console")
    assert response.status_code == 401


def test_console_with_the_wrong_token_is_refused() -> None:
    client = TestClient(create_app(operator_token=TOKEN))
    response = client.get("/console?token=wrong")
    assert response.status_code == 401


def test_console_with_the_right_token_renders_and_sets_a_cookie() -> None:
    client = TestClient(create_app(operator_token=TOKEN))
    response = client.get(f"/console?token={TOKEN}")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert any(c.name == "cua_operator" for c in client.cookies.jar)


def test_the_cookie_alone_is_enough_on_a_second_visit() -> None:
    client = TestClient(create_app(operator_token=TOKEN))
    client.get(f"/console?token={TOKEN}")
    second = client.get("/console")
    assert second.status_code == 200


def test_console_with_no_live_sessions_renders_an_empty_table() -> None:
    client = TestClient(create_app(operator_token=TOKEN))
    response = client.get(f"/console?token={TOKEN}")
    assert "Interventions" in response.text
    assert "/console/interventions/" not in response.text


def test_the_console_cookie_does_not_grant_access_to_the_json_api() -> None:
    """The brief's ruling, locked in: the console cookie is a convenience layer over the
    JSON API, not a second auth mechanism for it -- `/interventions/*` stays bearer-only."""
    client = TestClient(create_app(operator_token=TOKEN))
    client.get(f"/console?token={TOKEN}")
    assert any(c.name == "cua_operator" for c in client.cookies.jar)
    response = client.post("/interventions/iv-nope/claim", json={"operator_id": "op-1"})
    assert response.status_code == 401


def test_console_intervention_detail_requires_a_token_or_cookie() -> None:
    client = TestClient(create_app(operator_token=TOKEN))
    response = client.get("/console/interventions/iv-nope")
    assert response.status_code == 401


def test_console_intervention_detail_404s_for_an_unknown_id() -> None:
    client = TestClient(create_app(operator_token=TOKEN))
    response = client.get(f"/console/interventions/iv-nope?token={TOKEN}")
    assert response.status_code == 404
