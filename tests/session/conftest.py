"""Helpers shared by this package's live session-service modules (`test_service.py` and
`test_handoff_integration.py`): the operator token, a per-test policy file pointed at
`live_mockapp`'s own dynamic origin, the `POST /sessions` body, the approved-registry save an
irreversible step needs, and the poll-with-a-deadline waits every live test uses. Plain
functions, imported by name -- the same precedent `tests/replay/conftest.py` set.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cua.artifact.models import Artifact, Matcher, Provenance
from cua.artifact.store import RegistryEntry, save, write_registry_entry
from cua.session.service import create_app

TOKEN = "test-operator-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# An anonymous `GET /search` redirects to `/login`, and the mock app renders no heading on
# either page -- so a step that must settle cleanly matches the login page's own submit
# button, the one thing every anonymous navigation to `/search` really lands on.
_LOGIN_PAGE = Matcher(role="button", name_match="exact", name="Sign in")


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(operator_token=TOKEN))


def _write_policy(tmp_path, origin: str) -> str:
    # Review finding #11: `policy.example.yaml` names a fixed origin that can never match
    # `live_mockapp`'s randomly-assigned port -- every session in this package would freeze
    # before ever escalating if the tests pointed at it. Write a real policy file per test,
    # matching the PolicyConfig/DeploymentAllowlist YAML shape `policy.example.yaml` itself
    # documents, with the live mock app's own dynamic origin.
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        "policy_mode: sandbox\n"
        f"allowed_origins:\n  - {origin}\n"
        "allowed_paths:\n  - /\n"
        "denied_paths: []\n"
        "allowed_actions:\n  - navigate\n  - click\n  - fill\n  - select\n  - press_key\n"
        "  - wait_for\n  - read\n  - dismiss_dialog\n"
    )
    return str(policy_path)


def _provenance(run_id: str) -> Provenance:
    return Provenance(discovered_at="2026-09-09T00:00:00", model="gemini-2.5-flash-lite",
                      policy_mode="sandbox", provider_retention="training_permitted",
                      run_id=run_id, trace_ref=f"evidence/{run_id}/trace.jsonl")


def _save_approved(tmp_path, artifact: Artifact) -> Artifact:
    """Review finding #12: an artifact with an `irreversible` step is refused `POLICY_BLOCKED`
    by `_policy_gate`'s own D41 check unless its registry entry is `approved` -- saved once
    (the (id, version) path is immutable), then approved explicitly."""
    save(artifact, tmp_path)
    write_registry_entry(tmp_path, artifact.id, artifact.version,
                         RegistryEntry(status="approved"), artifact=artifact)
    return artifact


def _session_body(tmp_path, live_mockapp, artifact: Artifact, **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "artifact_id": artifact.id, "version": artifact.version, "root": str(tmp_path),
        "base_url": live_mockapp, "policy_path": _write_policy(tmp_path, live_mockapp),
        "inputs": {}, "ttl_ms": 5000, "claim_ttl_ms": 10000,
    }
    body.update(over)
    return body


def _poll(fetch: Callable[[], Any], *, timeout_s: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = fetch()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"nothing arrived within {timeout_s}s")


def _poll_result(client: TestClient, session_id: str) -> dict[str, Any]:
    result: dict[str, Any] = _poll(lambda: client.get(f"/sessions/{session_id}").json()["result"])
    return result


def _poll_interventions(client: TestClient, session_id: str, *, count: int = 1) -> list[dict]:
    def fetch() -> list[dict] | None:
        listing: list[dict] = client.get(f"/sessions/{session_id}/interventions").json()
        return listing if len(listing) >= count else None
    interventions: list[dict] = _poll(fetch)
    return interventions
