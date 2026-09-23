"""Spec §7.3-§7.4, §7.7: the pure half of intervention lifecycle -- creation, claiming, and
expiry, proven with an injected Clock so no test here touches real time. The live half (a
real threading.Event backing an actual wait) is Task 6/8's job; this module is what that
wiring sits on top of.
"""
import pytest

from cua.session.interventions import Intervention, InterventionExpired, Interventions
from tests.replay.conftest import FakeClock


def _create(interventions: Interventions, **over) -> Intervention:
    fields = dict(
        run_id="run-test", goal="close the account", capability_id="corebank.probe",
        version=1, step_id="s3", reason_code="NO_BRANCH_MATCHED",
        expected="one of: continue on heading 'Member '", observed="observed: nothing",
        screenshot_ref="evidence/run-test/screenshots/s3.png",
        snapshot_ref="evidence/run-test/snapshots/s3.yaml",
        allowed_operator_actions=["resolved", "resolved_manually", "cannot_resolve"],
        ttl_ms=60_000, claim_ttl_ms=120_000,
    )
    fields.update(over)
    return interventions.create(**fields)


def test_create_returns_an_open_intervention_with_every_field_set() -> None:
    clock = FakeClock()
    interventions = Interventions(clock=clock)
    iv = _create(interventions)
    assert iv.status == "open"
    assert iv.claimed_by is None
    assert iv.id
    assert iv.run_id == "run-test"
    assert iv.reason_code == "NO_BRANCH_MATCHED"
    assert iv.created_at == clock.monotonic_ms()


def test_get_returns_none_for_an_unknown_id() -> None:
    interventions = Interventions(clock=FakeClock())
    assert interventions.get("nope") is None


def test_list_all_returns_every_intervention_in_creation_order() -> None:
    interventions = Interventions(clock=FakeClock())
    assert interventions.list_all() == []
    first = _create(interventions, run_id="run-a")
    second = _create(interventions, run_id="run-b")
    assert interventions.list_all() == [first, second]


def test_claim_records_the_operator_and_moves_to_claimed() -> None:
    clock = FakeClock()
    interventions = Interventions(clock=clock)
    iv = _create(interventions)
    claimed = interventions.claim(iv.id, "op-1", clock=clock)
    assert claimed.status == "claimed"
    assert claimed.claimed_by == "op-1"


def test_claim_after_the_ttl_has_elapsed_raises_and_does_not_claim() -> None:
    clock = FakeClock()
    interventions = Interventions(clock=clock)
    iv = _create(interventions, ttl_ms=1000)
    clock.sleep_ms(1000)
    with pytest.raises(InterventionExpired):
        interventions.claim(iv.id, "op-1", clock=clock)
    assert interventions.get(iv.id).status == "expired"  # type: ignore[union-attr]


def test_expire_if_due_moves_an_unclaimed_intervention_past_its_ttl() -> None:
    clock = FakeClock()
    interventions = Interventions(clock=clock)
    iv = _create(interventions, ttl_ms=1000)
    assert interventions.expire_if_due(iv.id, clock=clock) is False
    clock.sleep_ms(1000)
    assert interventions.expire_if_due(iv.id, clock=clock) is True
    assert interventions.get(iv.id).status == "expired"  # type: ignore[union-attr]


def test_expire_if_due_leaves_a_claim_alone_until_its_own_claim_ttl() -> None:
    # Acceptance criterion 3: a claim holds the ttl -- an operator actively working an
    # intervention must not have it expire out from under them.
    clock = FakeClock()
    interventions = Interventions(clock=clock)
    iv = _create(interventions, ttl_ms=1000, claim_ttl_ms=5000)
    interventions.claim(iv.id, "op-1", clock=clock)
    clock.sleep_ms(1000)  # past the original ttl, but claimed
    assert interventions.expire_if_due(iv.id, clock=clock) is False
    assert interventions.get(iv.id).status == "claimed"  # type: ignore[union-attr]


def test_an_abandoned_claim_expires_on_its_own_claim_ttl() -> None:
    clock = FakeClock()
    interventions = Interventions(clock=clock)
    iv = _create(interventions, ttl_ms=1000, claim_ttl_ms=5000)
    interventions.claim(iv.id, "op-1", clock=clock)
    clock.sleep_ms(5000)
    assert interventions.expire_if_due(iv.id, clock=clock) is True
    assert interventions.get(iv.id).status == "expired"  # type: ignore[union-attr]


def test_handback_moves_a_claimed_intervention_to_returned() -> None:
    clock = FakeClock()
    interventions = Interventions(clock=clock)
    iv = _create(interventions)
    interventions.claim(iv.id, "op-1", clock=clock)
    returned = interventions.handback(iv.id)
    assert returned.status == "returned"


def test_handback_on_an_unclaimed_intervention_is_refused() -> None:
    interventions = Interventions(clock=FakeClock())
    iv = _create(interventions)
    with pytest.raises(ValueError, match="not claimed"):
        interventions.handback(iv.id)


def test_a_run_may_escalate_more_than_once_and_each_gets_its_own_id() -> None:
    # Spec §7.7: interventions are sequenced in the run log, plural.
    clock = FakeClock()
    interventions = Interventions(clock=clock)
    first = _create(interventions, step_id="s3")
    second = _create(interventions, step_id="s5")
    assert first.id != second.id
    assert interventions.get(first.id) is not None
    assert interventions.get(second.id) is not None
