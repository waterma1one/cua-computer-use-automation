"""Escalation demo: a real replay stalls, a human takes the live session, hands it back.

Run against a mock app on 127.0.0.1:8811 (`python -m mockapp base`), from the repo root:

    PYTHONPATH=. .venv/bin/python scripts/handoff_demo.py --policy <policy.yaml>

The capability is `mockcu.lookup_member_savings_balance` v2 (approved). The run is started with a
wrong password, so the sign-in never reaches the member search page and step s4 cannot settle:
the engine raises an intervention. The service, the engine, the lease and the browser are the
production ones, driven through the real HTTP routes. The one stand-in is the human: in
`cua serve` the operator works the headed browser; here a scripted operator signs in with the
right password inside the window a claim opens, so the run can be reproduced without hands.
That stand-in is the same one `tests/session/test_handoff_integration.py` uses.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from playwright.sync_api import Page

import cua.session.service as service_module
from cua.session.actions import HumanActionBracket
from cua.surface.web import SessionBrowser, open_session_page

TOKEN = "demo-operator-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BASE_URL = "http://127.0.0.1:8811"


def _poll(fetch: Any, timeout_s: float = 30.0) -> Any:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = fetch()
        if value:
            return value
        time.sleep(0.1)
    raise SystemExit("timed out waiting for the session")


def _install_scripted_operator(done: threading.Event, password: str) -> None:
    pages: dict[int, Page] = {}

    def open_and_remember(base_url: str, *, headless: bool = True) -> SessionBrowser:
        browser = open_session_page(base_url, headless=headless)
        pages[threading.get_ident()] = browser.page
        return browser

    class _Window(HumanActionBracket):
        def before(self, surface: Any, sink: Any) -> None:
            super().before(surface, sink)
            page = pages[threading.get_ident()]
            frame = page.frame(name="content")
            assert frame is not None
            frame.get_by_role("textbox", name="User", exact=True).fill("teller")
            frame.get_by_role("textbox", name="Password", exact=True).fill(password)
            frame.get_by_role("button", name="Sign in", exact=True).click()
            page.wait_for_load_state("networkidle")
            done.set()

    service_module.open_session_page = open_and_remember  # type: ignore[assignment]
    service_module.HumanActionBracket = _Window  # type: ignore[assignment,misc]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--member-id", default="12345")
    parser.add_argument("--password", default="teller-demo-pw")
    args = parser.parse_args()

    done = threading.Event()
    _install_scripted_operator(done, args.password)
    client = TestClient(service_module.create_app(operator_token=TOKEN))
    body = {
        "artifact_id": "mockcu.lookup_member_savings_balance", "version": 2,
        "root": str(Path(args.root).resolve()), "base_url": BASE_URL,
        "policy_path": str(Path(args.policy).resolve()),
        "inputs": {"username": "teller", "password": "wrong-password",
                   "member_id": args.member_id},
        "ttl_ms": 20_000, "claim_ttl_ms": 60_000,
    }
    sid = client.post("/sessions", json=body, headers=AUTH).json()["session_id"]
    [iv] = _poll(lambda: client.get(f"/sessions/{sid}/interventions", headers=AUTH).json())
    fields = ("id", "step_id", "reason_code", "status")
    print("intervention:", json.dumps({k: iv[k] for k in fields}))
    client.post(f"/interventions/{iv['id']}/claim", json={"operator_id": "op-1"}, headers=AUTH)
    print("controller after claim:",
          client.get(f"/sessions/{sid}", headers=AUTH).json()["controller"])
    if not done.wait(timeout=30):
        raise SystemExit("the operator window never opened")
    client.post(f"/interventions/{iv['id']}/handback", json={"outcome": "resolved"}, headers=AUTH)
    result = _poll(lambda: client.get(f"/sessions/{sid}", headers=AUTH).json()["result"])
    print("result:", json.dumps(result))


if __name__ == "__main__":
    main()
