from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from cua.artifact.store import read_registry, save
from cua.artifact.validate import DeploymentAllowlist
from cua.catalog.invoke import IdempotencyLedger, InterventionRequested, invoke
from cua.catalog.registry import CatalogRefusal, approve
from cua.replay.result import Success
from cua.session.interventions import Interventions
from tests.artifact.factories import base

DAY_MS = 24 * 60 * 60 * 1000


@dataclass
class FakeClock:
    now: int = 0

    def monotonic_ms(self) -> int:
        return self.now

    def sleep_ms(self, ms: int) -> None:
        self.now += ms


@dataclass
class Opened:
    """A surface factory that records whether anything tried to open a surface."""

    opened: int = 0
    seen: list[Any] = field(default_factory=list)

    @contextmanager
    def __call__(self, allowlist: DeploymentAllowlist) -> Iterator[Any]:
        self.opened += 1
        yield object()


def _deployment() -> DeploymentAllowlist:
    return DeploymentAllowlist(
        allowed_origins=["http://127.0.0.1:1"], allowed_paths=["/"], denied_paths=[],
        allowed_actions=["navigate", "click", "fill", "select", "press_key", "wait_for",
                         "read", "dismiss_dialog"],
    )


def _env(tmp_path: Any) -> tuple[Opened, Interventions, IdempotencyLedger, FakeClock]:
    clock = FakeClock()
    return Opened(), Interventions(clock=clock), IdempotencyLedger(clock), clock


def test_invoke_of_a_draft_is_refused_and_names_cua_approve(tmp_path) -> None:
    save(base(), tmp_path)
    factory, interventions, ledger, _ = _env(tmp_path)
    with pytest.raises(CatalogRefusal) as exc:
        invoke(tmp_path, "corebank.probe", {}, deployment=_deployment(),
               surface_factory=factory, interventions=interventions, ledger=ledger)
    assert "cua approve corebank.probe 1" in str(exc.value)
    assert factory.opened == 0


def test_a_missing_capability_is_refused(tmp_path) -> None:
    factory, interventions, ledger, _ = _env(tmp_path)
    with pytest.raises(CatalogRefusal):
        invoke(tmp_path, "nope.nothing", {}, deployment=_deployment(),
               surface_factory=factory, interventions=interventions, ledger=ledger)


def _approved_irreversible(tmp_path) -> None:
    artifact = base()
    artifact.steps[0].risk = "irreversible"
    save(artifact, tmp_path)
    approve(tmp_path, artifact.id, 1, "ops")


def test_irreversible_invoke_creates_an_intervention_and_executes_nothing(tmp_path) -> None:
    _approved_irreversible(tmp_path)
    factory, interventions, ledger, _ = _env(tmp_path)
    out = invoke(tmp_path, "corebank.probe", {}, deployment=_deployment(),
                 surface_factory=factory, interventions=interventions, ledger=ledger,
                 confirm_irreversible=True, idempotency_key="k1")
    assert isinstance(out, InterventionRequested)
    assert factory.opened == 0  # the request-not-execute rule: no surface was touched
    iv = interventions.get(out.intervention.id)
    assert iv is not None
    assert iv.reason_code == "POLICY_BLOCKED"
    assert iv.acted is False
    assert iv.allowed_operator_actions == ["approve", "deny"]
    assert iv.capability_id == "corebank.probe" and iv.version == 1
    # a request is not a replay: stability is untouched
    assert read_registry(tmp_path)["corebank.probe"]["1"].replays == 0


def test_duplicate_idempotency_key_inside_the_window_is_refused(tmp_path) -> None:
    # This protects OUR side only: it stops this catalog from raising the same request twice
    # within 24h, in this process. It does nothing about the downstream system, which has no
    # idea about our key, and it is lost when the process exits.
    _approved_irreversible(tmp_path)
    factory, interventions, ledger, clock = _env(tmp_path)

    def call(key: str) -> object:
        return invoke(tmp_path, "corebank.probe", {}, deployment=_deployment(),
                      surface_factory=factory, interventions=interventions, ledger=ledger,
                      idempotency_key=key)

    call("k1")
    with pytest.raises(CatalogRefusal):
        call("k1")
    call("k2")  # a different key is a different request
    clock.now = DAY_MS - 1
    with pytest.raises(CatalogRefusal):
        call("k1")
    clock.now = DAY_MS + 1  # the window has passed
    call("k1")
    assert len(interventions.list_all()) == 3


def test_an_approved_safe_capability_is_replayed_and_recorded(tmp_path, monkeypatch) -> None:
    save(base(), tmp_path)
    approve(tmp_path, "corebank.probe", 1, "ops")
    factory, interventions, ledger, _ = _env(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_replay(artifact: Any, inputs: Any, surface: Any, mode: Any, **kw: Any) -> Success:
        calls.append(kw)
        return Success(outputs={}, steps_run=["s1"], evidence_ref="evidence/r")

    monkeypatch.setattr("cua.catalog.invoke.run_replay", fake_replay)
    out = invoke(tmp_path, "corebank.probe", {}, deployment=_deployment(),
                 surface_factory=factory, interventions=interventions, ledger=ledger)
    assert isinstance(out, Success)
    assert factory.opened == 1
    assert calls[0]["status"] == "approved"
    entry = read_registry(tmp_path)["corebank.probe"]["1"]
    assert (entry.replays, entry.successes, entry.score) == (1, 1, 1.0)
