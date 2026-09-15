import time

from cua.artifact.models import Expect, Matcher, Recovery, Settle, Step
from cua.replay.settle import Business, Continue, DialogUnhandled, Escalate, Fail, TimedOut, settle
from tests.artifact.factories import loc
from tests.replay.conftest import FakeClock, FakeSurface, node

SPEC = Settle(timeout_ms=8000, poll_ms=200)


def _step() -> Step:
    return Step(
        id="s3", action="click", locator=loc("Search"), risk="safe",
        expects=[
            Expect(when=Matcher(strategy="text", name_match="contains", name="No member found"),
                   outcome="business", code="MEMBER_NOT_FOUND", source="observed"),
            Expect(when=Matcher(role="heading", name_match="contains", name="Member "),
                   outcome="continue", source="observed"),
        ],
    )


def test_a_continue_clause_yields_continue() -> None:
    surface = FakeSurface(frames=[[node("heading", name="Member 12345")]])
    outcome = settle(surface, _step(), SPEC, [], clock=FakeClock())
    assert isinstance(outcome, Continue)


def test_a_business_clause_yields_business_with_its_code() -> None:
    surface = FakeSurface(frames=[[node("text", value="No member found")]])
    outcome = settle(surface, _step(), SPEC, [], clock=FakeClock())
    assert isinstance(outcome, Business)
    assert outcome.code == "MEMBER_NOT_FOUND"


def test_a_matched_fail_clause_yields_fail_with_its_declared_kind() -> None:
    # E4': a fail clause's code is a FailureKind, carried straight through by settle.
    step = Step(id="s3", action="click", locator=loc("Search"), risk="safe", expects=[
        Expect(when=Matcher(strategy="text", name_match="contains", name="Session Expired"),
               outcome="fail", code="SESSION_LOST", source="observed"),
    ])
    surface = FakeSurface(frames=[[node("text", value="Session Expired")]])
    outcome = settle(surface, step, SPEC, [], clock=FakeClock())
    assert isinstance(outcome, Fail)
    assert outcome.code == "SESSION_LOST"


def test_a_step_with_no_expects_completes_immediately() -> None:
    # A plain fill/navigate step with nothing declared to wait for must not poll to
    # timeout -- only a declared expects clause is something to wait for.
    step = Step(id="s1", action="fill", locator=loc("Member ID"),
               value={"literal": "12345"}, risk="safe")
    surface = FakeSurface(frames=[[node("button", name="Member ID")]])
    clock = FakeClock()
    outcome = settle(surface, step, SPEC, [], clock=clock)
    assert isinstance(outcome, Continue)
    assert clock.sleep_calls == []


def test_recovery_is_evaluated_before_expects_on_every_poll() -> None:
    step = _step()
    recovery = [Recovery(
        name="interstitial_notice",
        detect=Matcher(strategy="text", name_match="contains", name="Scheduled maintenance"),
        handle="escalate",
    )]
    surface = FakeSurface(frames=[[
        node("text", value="Scheduled maintenance"),
        node("heading", name="Member 12345"),
    ]])
    outcome = settle(surface, step, SPEC, recovery, clock=FakeClock())
    assert isinstance(outcome, Escalate)
    assert outcome.recovery_name == "interstitial_notice"


def test_ties_between_expects_clauses_are_broken_by_declaration_order() -> None:
    frame = [node("text", value="No member found"), node("heading", name="Member 12345")]
    forward = Step(id="s3", action="click", locator=loc("Search"), risk="safe", expects=[
        Expect(when=Matcher(strategy="text", name_match="contains", name="No member found"),
               outcome="business", code="MEMBER_NOT_FOUND", source="observed"),
        Expect(when=Matcher(role="heading", name_match="contains", name="Member "),
               outcome="continue", source="observed"),
    ])
    reversed_step = Step(id="s3", action="click", locator=loc("Search"), risk="safe", expects=[
        Expect(when=Matcher(role="heading", name_match="contains", name="Member "),
               outcome="continue", source="observed"),
        Expect(when=Matcher(strategy="text", name_match="contains", name="No member found"),
               outcome="business", code="MEMBER_NOT_FOUND", source="observed"),
    ])
    first = settle(FakeSurface(frames=[frame]), forward, SPEC, [], clock=FakeClock())
    second = settle(FakeSurface(frames=[frame]), reversed_step, SPEC, [], clock=FakeClock())
    assert isinstance(first, Business) and first.code == "MEMBER_NOT_FOUND"
    assert isinstance(second, Continue)


def test_timeout_with_no_match_yields_timed_out_carrying_the_last_observation() -> None:
    surface = FakeSurface(frames=[[node("text", value="nothing recognizable")]])
    outcome = settle(surface, _step(), SPEC, [], clock=FakeClock())
    assert isinstance(outcome, TimedOut)
    assert outcome.observation.nodes[0].value == "nothing recognizable"


def test_the_poll_loop_never_sleeps_for_real_even_across_a_full_timeout() -> None:
    clock = FakeClock()
    surface = FakeSurface(frames=[[node("text", value="never matches")]])
    started = time.perf_counter()
    outcome = settle(surface, _step(), SPEC, [], clock=clock)
    elapsed = time.perf_counter() - started
    assert isinstance(outcome, TimedOut)
    assert clock.sleep_calls == [200] * 40  # 8000ms / 200ms
    assert elapsed < 0.5, "settle() must not perform a real sleep"


def test_a_matched_retry_clause_keeps_polling_rather_than_ending_the_step() -> None:
    step = Step(id="s3", action="click", locator=loc("Search"), risk="safe", expects=[
        Expect(when=Matcher(strategy="text", name_match="contains", name="Please wait"),
               outcome="retry", source="observed"),
        Expect(when=Matcher(role="heading", name_match="contains", name="Member "),
               outcome="continue", source="observed"),
    ])
    surface = FakeSurface(frames=[
        [node("text", value="Please wait")],
        [node("text", value="Please wait")],
        [node("heading", name="Member 12345")],
    ])
    outcome = settle(surface, step, SPEC, [], clock=FakeClock())
    assert isinstance(outcome, Continue)


def test_an_unhandled_dialog_is_reported_when_no_recovery_rule_matches_it() -> None:
    surface = FakeSurface(frames=[[node("heading", name="Member 12345")]],
                          dialog_messages=["Unexpected dialog"])
    outcome = settle(surface, _step(), SPEC, [], clock=FakeClock())
    assert isinstance(outcome, DialogUnhandled)
    assert outcome.message == "Unexpected dialog"


def test_a_dialog_matching_a_dismiss_recovery_rule_is_dismissed_and_polling_continues() -> None:
    recovery = [Recovery(name="stray_confirm",
                         detect=Matcher(strategy="text", name_match="contains", name="Unexpected"),
                         handle="dismiss")]
    surface = FakeSurface(
        frames=[[node("heading", name="Member 12345")], [node("heading", name="Member 12345")]],
        dialog_messages=["Unexpected dialog", None],
    )
    outcome = settle(surface, _step(), SPEC, recovery, clock=FakeClock())
    assert isinstance(outcome, Continue)
    assert surface.dismiss_calls == 1


def test_a_dismissed_dialog_that_never_clears_still_respects_the_deadline() -> None:
    # E26: a dismiss rule matching a dialog that keeps reappearing must not spin
    # unboundedly -- it falls through to the same deadline check and poll sleep as any
    # other absorbed iteration, rather than looping back to the top immediately.
    recovery = [Recovery(name="stray_confirm",
                         detect=Matcher(strategy="text", name_match="contains", name="Unexpected"),
                         handle="dismiss")]
    surface = FakeSurface(frames=[[]], dialog_messages=["Unexpected dialog"])
    clock = FakeClock()
    outcome = settle(surface, _step(), SPEC, recovery, clock=clock)
    assert isinstance(outcome, TimedOut)
    assert clock.sleep_calls == [200] * 40  # 8000ms / 200ms
    assert surface.dismiss_calls == 41  # one dismiss per poll, including the final one


def test_first_match_wins_over_expects_even_when_the_match_is_a_retry() -> None:
    step = Step(id="s3", action="click", locator=loc("Search"), risk="safe", expects=[
        Expect(when=Matcher(strategy="text", name_match="contains", name="Please wait"),
               outcome="retry", source="observed"),
        Expect(when=Matcher(role="heading", name_match="contains", name="Member "),
               outcome="continue", source="observed"),
    ])
    frame = [node("text", value="Please wait"), node("heading", name="Member 12345")]
    outcome = settle(FakeSurface(frames=[frame]), step, SPEC, [], clock=FakeClock())
    assert isinstance(outcome, TimedOut)


def test_first_match_wins_over_expects_the_other_way_too() -> None:
    step = Step(id="s3", action="click", locator=loc("Search"), risk="safe", expects=[
        Expect(when=Matcher(role="heading", name_match="contains", name="Member "),
               outcome="continue", source="observed"),
        Expect(when=Matcher(strategy="text", name_match="contains", name="Please wait"),
               outcome="retry", source="observed"),
    ])
    frame = [node("text", value="Please wait"), node("heading", name="Member 12345")]
    clock = FakeClock()
    outcome = settle(FakeSurface(frames=[frame]), step, SPEC, [], clock=clock)
    assert isinstance(outcome, Continue)
    assert clock.sleep_calls == []


def test_first_match_wins_over_recovery_even_when_the_match_is_a_dismiss() -> None:
    detect = Matcher(strategy="text", name_match="contains", name="Scheduled maintenance")
    recovery = [
        Recovery(name="ignore_it", detect=detect, handle="dismiss"),
        Recovery(name="escalate_it", detect=detect, handle="escalate"),
    ]
    step = Step(id="s1", action="fill", locator=loc("Member ID"),
               value={"literal": "12345"}, risk="safe")
    surface = FakeSurface(frames=[[node("text", value="Scheduled maintenance")]])
    outcome = settle(surface, step, SPEC, recovery, clock=FakeClock())
    assert not isinstance(outcome, Escalate)
