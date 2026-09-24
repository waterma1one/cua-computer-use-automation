"""Spec §7: the session service, end to end at the HTTP layer, against a real `replay()` call
in a background thread. Uses short, real ttl_ms/claim_ttl_ms (E12: this is the one layer where
real wall-clock time is unavoidable, a threading.Event cannot take an injected clock) -- kept
in the low hundreds of milliseconds where a test waits for an expiry, so the suite stays fast.

Every wait below is a poll with a deadline, never a single fixed sleep: a session's thread
reaches its escalation only after a real Chromium launch, a navigation, and a settle timeout,
none of which has a fixed duration. Every test that leaves a session sitting in an escalation
wait ends it explicitly (a `cannot_resolve` handback, or a short ttl) so no browser outlives
the test that opened it by more than a moment.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from cua.artifact.models import (
    App,
    Artifact,
    Expect,
    InputSpec,
    Matcher,
    Settle,
    Step,
    Success,
    Target,
)
from cua.artifact.store import save
from cua.observability import EvidenceWriter
from cua.policy.config import load_policy
from cua.replay.settle import SYSTEM_CLOCK
from cua.session.lease import Lease, LeasedSurface
from cua.session.service import (
    SessionService,
    _EscalationTimeout,
    _HandbackRequest,
    _intervention_json,
    _LiveSession,
)
from cua.surface.models import EvidenceFrame
from tests.session.conftest import (
    _LOGIN_PAGE,
    AUTH,
    TOKEN,
    _poll_interventions,
    _poll_result,
    _provenance,
    _save_approved,
    _session_body,
    _write_policy,
)


def _artifact_that_always_escalates(tmp_path, *, version: int = 1) -> Artifact:
    artifact = Artifact(
        schema_version=1, id="corebank.handoff_probe", version=version, name="handoff_probe",
        description="never matches, to force an escalation every time", verified=False,
        app=App(vendor_product="corebank-teller", variant="base", surface="web", entry="/search"),
        settle=Settle(timeout_ms=300, poll_ms=50), max_duration_ms=60000,
        inputs={}, outputs={},
        steps=[Step(
            id="s1", action="navigate", target=Target(path="/search"), risk="safe",
            expects=[Expect(when=Matcher(role="heading", name_match="contains", name="Nope"),
                            outcome="continue", source="observed")],
        )],
        success=Success(checkpoint=Matcher(role="heading", name_match="contains", name="Nope")),
        provenance=_provenance("r_handoff"),
    )
    save(artifact, tmp_path)
    return artifact


def _three_step_artifact(tmp_path, *, s0_risk: str, version: int) -> Artifact:
    """s0 and s1 settle cleanly (on `_LOGIN_PAGE`); s2 never matches, so it is the
    step that escalates -- giving `restart_from` a two-step range ([s0, s2) or [s1, s2)) to
    be validated against."""
    search = Expect(when=_LOGIN_PAGE, outcome="continue", source="observed", verified=True)
    artifact = Artifact(
        schema_version=1, id="corebank.handoff_probe", version=version, name="handoff_probe",
        description="s0/s1 settle; s2 never matches, to force an escalation", verified=False,
        app=App(vendor_product="corebank-teller", variant="base", surface="web", entry="/search"),
        settle=Settle(timeout_ms=300, poll_ms=50), max_duration_ms=60000,
        inputs={}, outputs={},
        steps=[
            Step(id="s0", action="navigate", target=Target(path="/search"), risk=s0_risk,
                 expects=[search]),
            Step(id="s1", action="navigate", target=Target(path="/search"), risk="safe",
                 expects=[search]),
            Step(id="s2", action="navigate", target=Target(path="/search"), risk="safe",
                 expects=[Expect(when=Matcher(role="heading", name_match="contains", name="Nope"),
                                 outcome="continue", source="observed", verified=True)]),
        ],
        success=Success(checkpoint=Matcher(role="heading", name_match="contains", name="Nope")),
        provenance=_provenance("r_handoff3"),
    )
    _save_approved(tmp_path, artifact)
    return artifact


def _slow_then_normal_artifact(tmp_path, *, version: int) -> Artifact:
    """s0's own response is deliberately slow (paired with a monkeypatched
    `MOCKAPP_SLOW_FAULT_MS`); s1 follows immediately after. Both settle on the same
    login-page checkpoint and the run reaches `Success` on its own -- there is no
    escalation anywhere in this artifact, so nothing ever calls `escalate()`."""
    search = Expect(when=_LOGIN_PAGE, outcome="continue", source="observed", verified=True)
    artifact = Artifact(
        schema_version=1, id="corebank.handoff_probe", version=version, name="handoff_probe",
        description="s0 is slow; both steps settle cleanly and the run never escalates",
        verified=False,
        app=App(vendor_product="corebank-teller", variant="base", surface="web", entry="/search"),
        settle=Settle(timeout_ms=2000, poll_ms=50), max_duration_ms=60000,
        inputs={}, outputs={},
        steps=[
            Step(id="s0", action="navigate", target=Target(path="/search?fault=slow"),
                 risk="safe", expects=[search]),
            Step(id="s1", action="navigate", target=Target(path="/search"), risk="safe",
                 expects=[search]),
        ],
        success=Success(checkpoint=_LOGIN_PAGE),
        provenance=_provenance("r_handoff_park"),
    )
    save(artifact, tmp_path)
    return artifact


def _end(client: TestClient, iv_id: str) -> None:
    client.post(f"/interventions/{iv_id}/handback",
                json={"outcome": "cannot_resolve", "note": "test cleanup"}, headers=AUTH)


def test_post_sessions_returns_a_session_id_and_a_lease_token(
    client, tmp_path, live_mockapp,
) -> None:
    artifact = _artifact_that_always_escalates(tmp_path)
    response = client.post("/sessions", json=_session_body(
        tmp_path, live_mockapp, artifact, ttl_ms=500, claim_ttl_ms=1000), headers=AUTH)
    assert response.status_code == 201
    body = response.json()
    assert "session_id" in body and "lease_token" in body
    assert isinstance(body["lease_token"], str) and body["lease_token"]
    _poll_result(client, body["session_id"])


def test_an_unclaimed_intervention_expires_and_the_run_fails_escalation_timeout(
    client, tmp_path, live_mockapp,
) -> None:
    artifact = _artifact_that_always_escalates(tmp_path)
    response = client.post("/sessions", json=_session_body(
        tmp_path, live_mockapp, artifact, ttl_ms=300, claim_ttl_ms=1000), headers=AUTH)
    session_id = response.json()["session_id"]
    result = _poll_result(client, session_id)
    assert result["kind"] == "ESCALATION_TIMEOUT"
    [iv] = client.get(f"/sessions/{session_id}/interventions", headers=AUTH).json()
    assert iv["status"] == "expired"
    # The finished session released its lease, after its thread tore the browser down.
    assert client.get(f"/sessions/{session_id}", headers=AUTH).json()["controller"] == "none"


def test_claim_requires_the_operator_token(client, tmp_path, live_mockapp) -> None:
    artifact = _artifact_that_always_escalates(tmp_path)
    session = client.post(
        "/sessions", json=_session_body(tmp_path, live_mockapp, artifact),
        headers=AUTH,
    ).json()
    iv_id = _poll_interventions(client, session["session_id"])[0]["id"]
    unauthorized = client.post(f"/interventions/{iv_id}/claim", json={"operator_id": "op-1"})
    assert unauthorized.status_code == 401
    wrong = client.post(f"/interventions/{iv_id}/claim", json={"operator_id": "op-1"},
                        headers={"Authorization": "Bearer not-the-token"})
    assert wrong.status_code == 401
    authorized = client.post(f"/interventions/{iv_id}/claim", json={"operator_id": "op-1"},
                             headers=AUTH)
    assert authorized.status_code == 200
    assert authorized.json()["claimed_by"] == "op-1"
    # §7.4 "take control": a claim transfers the lease to the operator.
    response = client.get(f"/sessions/{session['session_id']}", headers=AUTH)
    assert response.json()["controller"] == "operator"
    again = client.post(f"/interventions/{iv_id}/claim", json={"operator_id": "op-2"},
                        headers=AUTH)
    assert again.status_code == 409
    _end(client, iv_id)
    assert _poll_result(client, session["session_id"])["kind"] != "ESCALATION_TIMEOUT"


def test_handback_requires_the_operator_token(client, tmp_path, live_mockapp) -> None:
    artifact = _artifact_that_always_escalates(tmp_path)
    session = client.post(
        "/sessions", json=_session_body(tmp_path, live_mockapp, artifact),
        headers=AUTH,
    ).json()
    iv_id = _poll_interventions(client, session["session_id"])[0]["id"]
    client.post(f"/interventions/{iv_id}/claim", json={"operator_id": "op-1"}, headers=AUTH)
    refused = client.post(f"/interventions/{iv_id}/handback", json={"outcome": "resolved"})
    assert refused.status_code == 401
    _end(client, iv_id)
    _poll_result(client, session["session_id"])


def test_a_claimed_interventions_handback_resolved_manually_ends_the_run_assisted(
    client, tmp_path, live_mockapp,
) -> None:
    artifact = _artifact_that_always_escalates(tmp_path)
    session = client.post(
        "/sessions", json=_session_body(tmp_path, live_mockapp, artifact),
        headers=AUTH,
    ).json()
    iv_id = _poll_interventions(client, session["session_id"])[0]["id"]
    client.post(f"/interventions/{iv_id}/claim", json={"operator_id": "op-1"}, headers=AUTH)
    handback = client.post(
        f"/interventions/{iv_id}/handback", json={"outcome": "resolved_manually"}, headers=AUTH,
    )
    assert handback.status_code == 200
    assert handback.json()["handback_outcome"] == {"outcome": "resolved_manually"}
    result = _poll_result(client, session["session_id"])
    assert result.get("assistance") == "human"  # E4: the EXISTING field, never a new one


def test_cannot_resolve_ends_the_run_as_a_failure_carrying_the_operators_note(
    client, tmp_path, live_mockapp,
) -> None:
    artifact = _artifact_that_always_escalates(tmp_path)
    session = client.post(
        "/sessions", json=_session_body(tmp_path, live_mockapp, artifact),
        headers=AUTH,
    ).json()
    iv_id = _poll_interventions(client, session["session_id"])[0]["id"]
    client.post(f"/interventions/{iv_id}/claim", json={"operator_id": "op-1"}, headers=AUTH)
    no_note = client.post(f"/interventions/{iv_id}/handback",
                          json={"outcome": "cannot_resolve"}, headers=AUTH)
    assert no_note.status_code == 400
    response = client.post(f"/interventions/{iv_id}/handback",
                           json={"outcome": "cannot_resolve", "note": "member record locked"},
                           headers=AUTH)
    assert response.status_code == 200
    result = _poll_result(client, session["session_id"])
    assert "member record locked" in result["observed"]


def test_resolved_transfers_the_lease_back_to_the_agent_and_the_run_resumes(
    client, tmp_path, live_mockapp,
) -> None:
    # The step never matches, so a `Resolved` handback resumes the same replay, whose
    # re-verification escalates again -- a second intervention, opened by the SAME thread
    # still inside the SAME `replay()` call, now holding the lease again (E18: `rebind`, not a
    # reconstructed wrapper nothing points to).
    artifact = _artifact_that_always_escalates(tmp_path)
    session = client.post(
        "/sessions", json=_session_body(tmp_path, live_mockapp, artifact),
        headers=AUTH,
    ).json()
    sid = session["session_id"]
    first = _poll_interventions(client, sid)[0]["id"]
    client.post(f"/interventions/{first}/claim", json={"operator_id": "op-1"}, headers=AUTH)
    assert client.get(f"/sessions/{sid}", headers=AUTH).json()["controller"] == "operator"
    response = client.post(f"/interventions/{first}/handback", json={"outcome": "resolved"},
                           headers=AUTH)
    assert response.status_code == 200
    listing = _poll_interventions(client, sid, count=2)
    assert client.get(f"/sessions/{sid}", headers=AUTH).json()["controller"] == "agent"
    assert listing[0]["status"] == "returned"
    assert listing[1]["status"] == "open"
    second = listing[1]["id"]
    client.post(f"/interventions/{second}/claim", json={"operator_id": "op-1"}, headers=AUTH)
    _end(client, second)
    result = _poll_result(client, sid)
    assert result["kind"] != "ESCALATION_TIMEOUT"
    # Both claims' bracket evidence survives: the second claim never overwrote the first's.
    run_dir = Path(tmp_path) / result["evidence_ref"]
    for n in (1, 2):
        for half in ("before", "after"):
            assert (run_dir / "screenshots" / f"escalation_{n}_human_{half}.png").exists()
            assert (run_dir / "snapshots" / f"escalation_{n}_human_{half}.yaml").exists()


def test_restart_from_a_non_safe_range_is_refused_before_it_reaches_the_engine(
    client, tmp_path, live_mockapp,
) -> None:
    # Review finding #12: the original draft called `.save()` twice on the same (id, version)
    # -- an immutable path, so the second call raised `FileExistsError` -- and never set the
    # registry to `approved`, so `_policy_gate`'s own D41 check would refuse the run with
    # `POLICY_BLOCKED` before a single step ran, regardless of what this test wants to prove.
    # Fixed: a SEPARATE artifact (its own version, saved once) with a real irreversible s0
    # and a real locator, its own registry entry explicitly approved, following
    # `tests/policy/test_enforcement_integration.py`'s `_artifact` helper's own construction
    # idiom rather than mutating `_artifact_that_always_escalates`'s output in place.
    artifact = Artifact(
        schema_version=1, id="corebank.handoff_probe", version=2, name="handoff_probe",
        description="s0 is irreversible; s1 never matches, to force an escalation",
        verified=False,
        app=App(vendor_product="corebank-teller", variant="base", surface="web", entry="/search"),
        settle=Settle(timeout_ms=300, poll_ms=50), max_duration_ms=60000,
        inputs={}, outputs={},
        steps=[
            Step(id="s0", action="navigate", target=Target(path="/search"), risk="irreversible",
                 expects=[Expect(when=_LOGIN_PAGE, outcome="continue", source="observed",
                                 verified=True)]),
            Step(id="s1", action="navigate", target=Target(path="/search"), risk="safe",
                 expects=[Expect(when=Matcher(role="heading", name_match="contains", name="Nope"),
                                 outcome="continue", source="observed", verified=True)]),
        ],
        success=Success(checkpoint=Matcher(role="heading", name_match="contains", name="Nope")),
        provenance=_provenance("r_handoff2"),
    )
    _save_approved(tmp_path, artifact)
    # An irreversible step is refused by `replay()`'s own E27 gate without both of these --
    # before any step runs, and so before anything could escalate.
    session = client.post("/sessions", json=_session_body(
        tmp_path, live_mockapp, artifact, confirm_irreversible=True,
        idempotency_key=f"restart-probe-{time.monotonic_ns()}",
    ), headers=AUTH).json()
    [iv] = _poll_interventions(client, session["session_id"])
    assert iv["step_id"] == "s1"
    assert "restart_from" not in iv["allowed_operator_actions"]
    iv_id = iv["id"]
    client.post(f"/interventions/{iv_id}/claim", json={"operator_id": "op-1"}, headers=AUTH)
    response = client.post(
        f"/interventions/{iv_id}/handback",
        json={"outcome": "restart_from", "step_id": "s0"},
        headers=AUTH,
    )
    assert response.status_code == 400
    assert "safe" in response.json()["detail"].lower()
    # Refused before anything changed: still claimed, the operator still holds the lease.
    [still] = client.get(f"/sessions/{session['session_id']}/interventions", headers=AUTH).json()
    assert still["status"] == "claimed"
    response = client.get(f"/sessions/{session['session_id']}", headers=AUTH)
    assert response.json()["controller"] == "operator"
    _end(client, iv_id)
    _poll_result(client, session["session_id"])


def test_restart_from_is_offered_and_accepted_only_for_an_all_safe_range(
    client, tmp_path, live_mockapp,
) -> None:
    # s0 irreversible, s1 safe, s2 escalates: [s1, s2) is safe, [s0, s2) is not.
    artifact = _three_step_artifact(tmp_path, s0_risk="irreversible", version=3)
    session = client.post("/sessions", json=_session_body(
        tmp_path, live_mockapp, artifact, confirm_irreversible=True,
        idempotency_key=f"restart-range-{time.monotonic_ns()}",
    ), headers=AUTH).json()
    sid = session["session_id"]
    [iv] = _poll_interventions(client, sid)
    assert iv["step_id"] == "s2"
    assert "restart_from" in iv["allowed_operator_actions"]
    client.post(f"/interventions/{iv['id']}/claim", json={"operator_id": "op-1"}, headers=AUTH)
    for bad in ({"outcome": "restart_from"},                       # no step_id
                {"outcome": "restart_from", "step_id": "nope"},    # not a step
                {"outcome": "restart_from", "step_id": "s2"},      # not before the escalation
                {"outcome": "restart_from", "step_id": "s0"},      # range holds irreversible s0
                {"outcome": "bogus"}):
        assert client.post(f"/interventions/{iv['id']}/handback", json=bad,
                           headers=AUTH).status_code == 400, bad
    accepted = client.post(f"/interventions/{iv['id']}/handback",
                           json={"outcome": "restart_from", "step_id": "s1"}, headers=AUTH)
    assert accepted.status_code == 200
    # The restart re-runs s1 then s2, which escalates again -- on the agent's own lease.
    listing = _poll_interventions(client, sid, count=2)
    assert listing[1]["step_id"] == "s2"
    assert client.get(f"/sessions/{sid}", headers=AUTH).json()["controller"] == "agent"
    client.post(f"/interventions/{listing[1]['id']}/claim", json={"operator_id": "op-1"},
                headers=AUTH)
    _end(client, listing[1]["id"])
    _poll_result(client, sid)


def test_restart_before_an_irreversible_step_that_already_acted_is_refused(
    client, tmp_path, live_mockapp,
) -> None:
    # s0 safe, s1 irreversible: s1's act() runs, its settle then fails, and the engine
    # escalates it anyway (trigger (c)). A restart from the safe s0 would re-run s1 -- a
    # second real irreversible act under the same run -- so it is neither offered nor
    # accepted.
    never = Expect(when=Matcher(role="heading", name_match="contains", name="Nope"),
                   outcome="continue", source="observed", verified=True)
    artifact = Artifact(
        schema_version=1, id="corebank.handoff_probe", version=4, name="handoff_probe",
        description="s1 is irreversible and acts before it fails to settle", verified=False,
        app=App(vendor_product="corebank-teller", variant="base", surface="web", entry="/search"),
        settle=Settle(timeout_ms=300, poll_ms=50), max_duration_ms=60000,
        inputs={}, outputs={},
        steps=[
            Step(id="s0", action="navigate", target=Target(path="/search"), risk="safe",
                 expects=[Expect(when=_LOGIN_PAGE, outcome="continue", source="observed",
                                 verified=True)]),
            Step(id="s1", action="navigate", target=Target(path="/search"), risk="irreversible",
                 expects=[never]),
        ],
        success=Success(checkpoint=Matcher(role="heading", name_match="contains", name="Nope")),
        provenance=_provenance("r_handoff4"),
    )
    _save_approved(tmp_path, artifact)
    session = client.post("/sessions", json=_session_body(
        tmp_path, live_mockapp, artifact, confirm_irreversible=True,
        idempotency_key=f"irreversible-acted-{time.monotonic_ns()}",
    ), headers=AUTH).json()
    [iv] = _poll_interventions(client, session["session_id"])
    assert iv["step_id"] == "s1"
    assert "restart_from" not in iv["allowed_operator_actions"]
    client.post(f"/interventions/{iv['id']}/claim", json={"operator_id": "op-1"}, headers=AUTH)
    refused = client.post(f"/interventions/{iv['id']}/handback",
                          json={"outcome": "restart_from", "step_id": "s0"}, headers=AUTH)
    assert refused.status_code == 400
    assert "safe" in refused.json()["detail"].lower()
    _end(client, iv["id"])
    _poll_result(client, session["session_id"])


def test_a_second_session_with_a_live_sessions_idempotency_key_is_refused(
    client, tmp_path, live_mockapp,
) -> None:
    # D34's gate is check-then-burn with nothing between; two concurrent sessions sharing a
    # key (a client retrying after a network error) could both pass it and both act.
    artifact = _three_step_artifact(tmp_path, s0_risk="irreversible", version=5)
    key = f"shared-{time.monotonic_ns()}"
    body = _session_body(tmp_path, live_mockapp, artifact, confirm_irreversible=True,
                         idempotency_key=key)
    first = client.post("/sessions", json=body, headers=AUTH)
    assert first.status_code == 201
    second = client.post("/sessions", json=body, headers=AUTH)
    assert second.status_code == 409
    assert key in second.json()["detail"]
    [iv] = _poll_interventions(client, first.json()["session_id"])
    client.post(f"/interventions/{iv['id']}/claim", json={"operator_id": "op-1"}, headers=AUTH)
    _end(client, iv["id"])
    _poll_result(client, first.json()["session_id"])
    # Released at teardown: the service no longer holds the key (the engine's own D34 gate,
    # which sees it burned, is what answers a reuse now -- before any step runs).
    third = client.post("/sessions", json=body, headers=AUTH)
    assert third.status_code == 201
    assert _poll_result(client, third.json()["session_id"])["kind"] == "POLICY_BLOCKED"


def test_delete_parks_a_claimed_session_capturing_before_it_releases_the_lease(
    client, tmp_path, live_mockapp,
) -> None:
    artifact = _artifact_that_always_escalates(tmp_path)
    session = client.post(
        "/sessions", json=_session_body(tmp_path, live_mockapp, artifact),
        headers=AUTH,
    ).json()
    sid = session["session_id"]
    iv_id = _poll_interventions(client, sid)[0]["id"]
    client.post(f"/interventions/{iv_id}/claim", json={"operator_id": "op-1"}, headers=AUTH)
    response = client.delete(f"/sessions/{sid}", headers=AUTH)
    assert response.status_code == 202
    result = _poll_result(client, sid)
    assert result["kind"] == "ESCALATION_TIMEOUT"
    assert "parked" in result["observed"]
    assert client.get(f"/sessions/{sid}", headers=AUTH).json()["controller"] == "none"
    run_dir = Path(tmp_path) / result["evidence_ref"]
    assert (run_dir / "screenshots" / "parked.png").exists()
    assert (run_dir / "snapshots" / "parked.yaml").exists()
    # The bracket's "before" half ran at the claim wake point, on the session's own thread.
    assert (run_dir / "screenshots" / "escalation_1_human_before.png").exists()


def test_parking_a_session_with_no_pending_escalation_still_stops_it(
    client, tmp_path, live_mockapp, monkeypatch,
) -> None:
    # I6: between escalations -- here, an artifact with no escalation anywhere in it, which
    # would otherwise reach Success on its own -- DELETE used to be a pure no-op: `parked`
    # was set but nothing ever read it again, and the run simply finished. `park()` now
    # rebinds the lease to "none" under the lock, so the agent's own next act() raises
    # LeaseError instead: s0's slow response (a monkeypatched MOCKAPP_SLOW_FAULT_MS) gives
    # this DELETE a wide, deterministic window before s1's own act() is ever attempted.
    monkeypatch.setenv("MOCKAPP_SLOW_FAULT_MS", "300")
    artifact = _slow_then_normal_artifact(tmp_path, version=6)
    session = client.post("/sessions", json=_session_body(tmp_path, live_mockapp, artifact),
                          headers=AUTH).json()
    sid = session["session_id"]
    response = client.delete(f"/sessions/{sid}", headers=AUTH)
    assert response.status_code == 202
    assert response.json() == {"session_id": sid, "parked": True}
    result = _poll_result(client, sid)
    assert "kind" in result  # a Failure -- Success carries no `kind` field at all
    assert result["kind"] in ("SESSION_LOST", "ESCALATION_TIMEOUT")
    assert client.get(f"/sessions/{sid}/interventions", headers=AUTH).json() == []
    assert client.get(f"/sessions/{sid}", headers=AUTH).json()["controller"] == "none"


def test_parking_an_unclaimed_intervention_stops_it_without_waiting_for_its_ttl(
    client, tmp_path, live_mockapp,
) -> None:
    # I6: parking during an unclaimed intervention used to do nothing until an operator
    # claimed it or its own ttl_ms elapsed -- here, a full minute, well past `_poll_result`'s
    # own 10s budget, so this test would time out on the old behaviour rather than pass
    # slowly. `park()` now wakes the claim-phase wait itself, and `escalate()`'s own guard
    # right after that wait parks cleanly instead of assuming a real claim happened.
    artifact = _artifact_that_always_escalates(tmp_path)
    session = client.post("/sessions", json=_session_body(
        tmp_path, live_mockapp, artifact, ttl_ms=60_000, claim_ttl_ms=60_000),
        headers=AUTH).json()
    sid = session["session_id"]
    iv_id = _poll_interventions(client, sid)[0]["id"]
    response = client.delete(f"/sessions/{sid}", headers=AUTH)
    assert response.status_code == 202
    assert response.json() == {"session_id": sid, "parked": True}
    result = _poll_result(client, sid)
    assert result["kind"] == "ESCALATION_TIMEOUT"
    assert "parked" in result["observed"]
    [iv] = client.get(f"/sessions/{sid}/interventions", headers=AUTH).json()
    assert iv["id"] == iv_id
    assert iv["status"] == "open"  # never claimed -- park pre-empted it, not a real claim
    assert client.get(f"/sessions/{sid}", headers=AUTH).json()["controller"] == "none"


def test_an_interventions_evidence_refs_name_files_the_session_actually_wrote(
    client, tmp_path, live_mockapp,
) -> None:
    artifact = _artifact_that_always_escalates(tmp_path)
    session = client.post(
        "/sessions", json=_session_body(tmp_path, live_mockapp, artifact),
        headers=AUTH,
    ).json()
    [iv] = _poll_interventions(client, session["session_id"])
    assert (Path(tmp_path) / iv["screenshot_ref"]).exists()
    assert (Path(tmp_path) / iv["snapshot_ref"]).exists()
    client.post(f"/interventions/{iv['id']}/claim", json={"operator_id": "op-1"}, headers=AUTH)
    _end(client, iv["id"])
    _poll_result(client, session["session_id"])


def test_the_run_log_durably_records_the_intervention_lifecycle_and_the_result(
    client, tmp_path, live_mockapp,
) -> None:
    # I4: spec §7.7 -- operator identity recorded on claim, interventions sequenced in the
    # run log -- and the same run.json/artifact.yaml/result.json triple `cua replay` writes,
    # so `Success(assistance="human")` and every other outcome survive the process instead
    # of living only in the in-memory `Intervention`/`_LiveSession` this test also checks.
    artifact = _artifact_that_always_escalates(tmp_path)
    session = client.post(
        "/sessions", json=_session_body(tmp_path, live_mockapp, artifact),
        headers=AUTH,
    ).json()
    sid = session["session_id"]
    iv_id = _poll_interventions(client, sid)[0]["id"]
    client.post(f"/interventions/{iv_id}/claim", json={"operator_id": "op-durable"}, headers=AUTH)
    client.post(f"/interventions/{iv_id}/handback", json={"outcome": "resolved_manually"},
               headers=AUTH)
    result = _poll_result(client, sid)
    assert result["assistance"] == "human"

    run_dir = Path(tmp_path) / result["evidence_ref"]
    assert (run_dir / "run.json").exists()
    assert (run_dir / "artifact.yaml").exists()
    written_result = json.loads((run_dir / "result.json").read_text())
    assert written_result["assistance"] == "human"

    events = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text().splitlines()]
    kinds = [e["kind"] for e in events]
    assert "intervention_created" in kinds
    assert "intervention_claimed" in kinds
    assert "intervention_handed_back" in kinds
    claimed = next(e for e in events if e["kind"] == "intervention_claimed")
    assert claimed["operator_id"] == "op-durable"
    handed_back = next(e for e in events if e["kind"] == "intervention_handed_back")
    assert handed_back["outcome"] == "resolved_manually"


def test_two_concurrent_sessions_each_resolve_against_their_own_interventions(
    client, tmp_path, live_mockapp,
) -> None:
    # The Risks section's named probe: `escalate()` finds its session through a thread-local,
    # so two sessions escalating at once must each block on, and be released by, only their
    # own intervention.
    artifact = _artifact_that_always_escalates(tmp_path)
    a = client.post(
        "/sessions", json=_session_body(tmp_path, live_mockapp, artifact),
        headers=AUTH,
    ).json()
    b = client.post(
        "/sessions", json=_session_body(tmp_path, live_mockapp, artifact),
        headers=AUTH,
    ).json()
    [iv_a] = _poll_interventions(client, a["session_id"])
    [iv_b] = _poll_interventions(client, b["session_id"])
    assert iv_a["id"] != iv_b["id"]
    assert iv_a["run_id"] == a["session_id"] and iv_b["run_id"] == b["session_id"]
    client.post(f"/interventions/{iv_b['id']}/claim", json={"operator_id": "op-b"}, headers=AUTH)
    client.post(f"/interventions/{iv_b['id']}/handback", json={"outcome": "resolved_manually"},
                headers=AUTH)
    assert _poll_result(client, b["session_id"])["assistance"] == "human"
    # Session A is untouched by B's whole lifecycle: still waiting, still unclaimed.
    assert client.get(f"/sessions/{a['session_id']}", headers=AUTH).json()["result"] is None
    [still_a] = client.get(f"/sessions/{a['session_id']}/interventions", headers=AUTH).json()
    assert still_a["status"] == "open"
    client.post(f"/interventions/{iv_a['id']}/claim", json={"operator_id": "op-a"}, headers=AUTH)
    _end(client, iv_a["id"])
    assert "test cleanup" in _poll_result(client, a["session_id"])["observed"]


def test_every_sessions_route_requires_the_operator_token(
    client, tmp_path, live_mockapp,
) -> None:
    # I5: POST /sessions, DELETE /sessions/{id}, GET /sessions/{id}, and
    # GET /sessions/{id}/interventions needed no token at all -- only claim/handback and the
    # console checked one. Every route in this router now reuses `_require_operator`, the
    # same helper claim/handback already used, rather than a second implementation of the
    # same check (D28).
    artifact = _artifact_that_always_escalates(tmp_path)
    body = _session_body(tmp_path, live_mockapp, artifact)

    assert client.post("/sessions", json=body).status_code == 401
    assert client.post("/sessions", json=body,
                       headers={"Authorization": "Bearer not-the-token"}).status_code == 401

    created = client.post("/sessions", json=body, headers=AUTH)
    assert created.status_code == 201
    sid = created.json()["session_id"]

    assert client.get(f"/sessions/{sid}").status_code == 401
    assert client.get(f"/sessions/{sid}", headers=AUTH).status_code == 200

    assert client.get(f"/sessions/{sid}/interventions").status_code == 401
    assert client.get(f"/sessions/{sid}/interventions", headers=AUTH).status_code == 200

    assert client.delete(f"/sessions/{sid}").status_code == 401  # never parks -- checked below
    response = client.delete(f"/sessions/{sid}", headers=AUTH)
    assert response.status_code == 202
    assert response.json() == {"session_id": sid, "parked": True}
    _poll_result(client, sid)


def test_unknown_ids_are_404(client) -> None:
    assert client.get("/sessions/nope", headers=AUTH).status_code == 404
    assert client.get("/sessions/nope/interventions", headers=AUTH).status_code == 404
    assert client.delete("/sessions/nope", headers=AUTH).status_code == 404
    assert client.post("/interventions/nope/claim", json={"operator_id": "op-1"},
                       headers=AUTH).status_code == 404
    assert client.post("/interventions/nope/handback", json={"outcome": "resolved"},
                       headers=AUTH).status_code == 404


def test_a_bad_policy_or_a_missing_artifact_is_refused_before_any_session_starts(
    client, tmp_path, live_mockapp,
) -> None:
    artifact = _artifact_that_always_escalates(tmp_path)
    missing_policy = client.post("/sessions", json=_session_body(
        tmp_path, live_mockapp, artifact, policy_path=str(tmp_path / "absent.yaml")), headers=AUTH)
    assert missing_policy.status_code == 400
    missing_artifact = client.post("/sessions", json=_session_body(
        tmp_path, live_mockapp, artifact, version=99), headers=AUTH)
    assert missing_artifact.status_code == 400


class _RecordedViolation:
    """Just enough of a `Surface` for `_ending_failure`: a recorded allowlist violation."""

    def __init__(self, reason: str | None) -> None:
        self.reason = reason

    def allowlist_violation(self) -> str | None:
        return self.reason


def _offline_session(tmp_path, reason: str | None, *, secret: str = "") -> _LiveSession:
    artifact = _artifact_that_always_escalates(tmp_path)
    if secret:
        artifact = artifact.model_copy(
            update={"inputs": {"pin": InputSpec(type="string", sensitive=True)}})
    lease = Lease()
    session = _LiveSession(
        session_id="sess-offline", artifact=artifact, base_url="http://127.0.0.1:1",
        root=Path(tmp_path),
        deployment=load_policy(Path(_write_policy(tmp_path, "http://127.0.0.1:1"))),
        status="draft", inputs={"pin": secret} if secret else {}, lease=lease,
        ttl_ms=1, claim_ttl_ms=1, logout_path=None,
    )
    session.surface = LeasedSurface(_RecordedViolation(reason), lease,  # type: ignore[arg-type]
                                    lease.acquire("agent"))
    session.writer = EvidenceWriter(Path(tmp_path))
    return session


class _DialogPendingSurface:
    """Just enough of a `Surface` for `escalate()`'s own capture guard (C1): a pending
    dialog, and a `capture()` that fails the test outright if it is ever called while one
    is pending -- a real `WebSurface.capture()` would instead block on `page.screenshot()`
    until Playwright's own ~30s default timeout and then raise `SurfaceError`, which is
    exactly the bug this guards against, so failing loudly here is the point.
    """

    def pending_dialog(self) -> str | None:
        return "Unexpected dialog"

    def capture(self):  # pragma: no cover -- must never be reached
        raise AssertionError("capture() must not be called while a dialog is pending")


def test_escalate_with_a_pending_dialog_creates_an_intervention_instead_of_raising(
    tmp_path,
) -> None:
    # C1: `escalate()` used to call `writer.frame(surface.capture(), ...)` unconditionally.
    # A pending dialog blocks `capture()` until Playwright's own timeout, which raised
    # `SurfaceError` here, escaped `escalate()` and `replay()`, and turned the whole run
    # into `SESSION_LOST` with no intervention ever created. `_offline_session`'s
    # `ttl_ms=1`/`claim_ttl_ms=1` make the claim phase expire at once, so this test proves
    # the fix without waiting on a real Playwright timeout: an intervention is created (the
    # escalation reached the human-facing half of `escalate()` at all), and the only
    # exception that comes out is the ordinary claim-phase expiry -- never a `SurfaceError`,
    # and never by way of `_DialogPendingSurface.capture()`, which would fail the test
    # directly if the guard were missing.
    service = SessionService(operator_token=TOKEN)
    session = _offline_session(tmp_path, None)
    session.surface = _DialogPendingSurface()  # type: ignore[assignment]
    service._local.session = session
    with pytest.raises(_EscalationTimeout):
        service.escalate(step_id="s1", kind="NO_BRANCH_MATCHED", expected="e", observed="o",
                         acted=True)
    [iv] = session.interventions.list_all()
    assert iv.step_id == "s1"
    assert iv.status == "expired"  # never claimed -- the claim-phase ttl elapsed, as scripted


class _NoDialogSurface:
    """Just enough of a `Surface` for `escalate()`'s own capture: no dialog pending, and a
    `capture()` that returns a real (empty) `EvidenceFrame`."""

    def pending_dialog(self) -> str | None:
        return None

    def capture(self) -> EvidenceFrame:
        return EvidenceFrame(generation=0, image_png=None, snapshot_yaml="")


def test_the_acted_flag_reaches_the_created_intervention_and_its_json(tmp_path) -> None:
    # I3: `escalate()`'s own `acted` parameter -- the engine's widening of `Escalator.
    # escalate`, negating whatever it computed as `pre_act` -- is recorded on the
    # `Intervention` itself, not just implied by its `reason_code`, so an operator (via
    # `GET /sessions/{id}/interventions`, `_intervention_json` here) can tell whether
    # choosing "resolved" re-runs an action that has not happened yet.
    service = SessionService(operator_token=TOKEN)
    session = _offline_session(tmp_path, None)
    session.surface = _NoDialogSurface()  # type: ignore[assignment]
    service._local.session = session
    with pytest.raises(_EscalationTimeout):
        service.escalate(step_id="s1", kind="LOCATOR_NOT_FOUND", expected="e", observed="o",
                         acted=False)
    [iv] = session.interventions.list_all()
    assert iv.acted is False
    assert _intervention_json(iv)["acted"] is False


@pytest.mark.parametrize("kind", ["ESCALATION_TIMEOUT", "SESSION_LOST"])
def test_a_recorded_violation_outranks_the_kind_a_session_would_otherwise_end_with(
    tmp_path, kind,
) -> None:
    # D40 at the service's own translation site: a violation the operator's drive time
    # tripped, followed by an expiry, a park, or a crash, is still ALLOWLIST_VIOLATION.
    service = SessionService(operator_token=TOKEN)
    session = _offline_session(tmp_path, "navigation to /account/close?pin=4321 was refused",
                               secret="4321")
    failure = service._ending_failure(session, kind, "expected", "observed")
    assert failure.kind == "ALLOWLIST_VIOLATION"
    assert "/account/close" in failure.observed
    assert "4321" not in failure.observed  # E31: a sensitive input never rides the reason


def test_with_no_recorded_violation_a_session_ends_with_its_own_kind(tmp_path) -> None:
    service = SessionService(operator_token=TOKEN)
    session = _offline_session(tmp_path, None)
    failure = service._ending_failure(session, "ESCALATION_TIMEOUT", "expected", "observed")
    assert (failure.kind, failure.observed) == ("ESCALATION_TIMEOUT", "observed")


def test_a_handback_after_the_sessions_thread_already_ended_is_a_409_not_a_crash(
    tmp_path,
) -> None:
    # A claimed intervention whose session thread then died (say, the bracket's capture
    # raised) has a released lease; a `resolved` handback must not try to transfer it.
    service = SessionService(operator_token=TOKEN)
    session = _offline_session(tmp_path, None)
    iv = session.interventions.create(
        run_id=session.session_id, goal="g", capability_id="c", version=1, step_id="s1",
        reason_code="NO_BRANCH_MATCHED", expected="e", observed="o", screenshot_ref="x",
        snapshot_ref="y", allowed_operator_actions=["resolved"], ttl_ms=60_000,
        claim_ttl_ms=60_000,
    )
    session.interventions.claim(iv.id, "op-1", clock=SYSTEM_CLOCK)
    session.lease.release()
    service._sessions[session.session_id] = session
    service._intervention_owner[iv.id] = session.session_id
    with pytest.raises(HTTPException) as refused:
        service.handback(iv.id, _HandbackRequest(outcome="resolved"))
    assert refused.value.status_code == 409
    assert session.interventions.get(iv.id).status == "claimed"
