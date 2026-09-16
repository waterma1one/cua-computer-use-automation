"""Spec §6.3: a heuristic, then a human. These tests pin the heuristic's tiers and, above
all, its direction of error.
"""
import pytest

from cua.policy.risk import RISK_ORDER, classify


def test_transfer_history_is_flagged_risky_and_that_is_the_point() -> None:
    # The asymmetry §6.3 states: this is a read-only link, and the heuristic flags it
    # anyway. Over-classifying costs a human a moment at approval time; the opposite
    # error -- a genuine transfer classified safe and executed unattended -- cannot be
    # undone. So the heuristic errs this way on purpose, and this test would be wrong to
    # "fix" by teaching it that History is harmless.
    assert classify("click", "Transfer History") == "risky"


@pytest.mark.parametrize("name", ["Post", "Delete", "Submit payment", "Close this account",
                                  "Close Account"])
def test_commit_verbs_on_a_fired_control_are_irreversible(name: str) -> None:
    assert classify("click", name) == "irreversible"
    assert classify("press_key", name) == "irreversible"


@pytest.mark.parametrize("name", ["Transfer", "Close", "Delete", "Post", "Submit payment"])
def test_the_regex_words_on_a_non_firing_action_are_risky_not_irreversible(name: str) -> None:
    # A `fill` into a field labelled "Transfer amount" is risky (it feeds a transfer) but
    # nothing has been committed yet.
    assert classify("fill", name) == "risky"


@pytest.mark.parametrize("name", ["Search", "Member ID", "Sign in", "Select", None])
def test_ordinary_controls_are_safe(name: str | None) -> None:
    assert classify("click", name) == "safe"
    assert classify("fill", name) == "safe"


@pytest.mark.parametrize("action", ["read", "wait_for"])
def test_observing_never_rises_above_safe_whatever_the_name(action: str) -> None:
    assert classify(action, "Post transfer") == "safe"  # type: ignore[arg-type]


def test_matching_is_case_insensitive_and_word_bounded() -> None:
    assert classify("click", "POST") == "irreversible"
    assert classify("click", "Postal code") == "safe"       # `post` inside a word
    assert classify("click", "Closed accounts") == "safe"   # `close` inside a word


def test_close_must_be_within_two_words_of_account_to_be_irreversible() -> None:
    # A bounded phrase: "Close this account" commits; a name that merely mentions an
    # account somewhere later does not jump to the heaviest gate -- it stays `risky`
    # because `close` alone is on the §6.3 list, and a human confirms from there.
    assert classify("click", "Close this account") == "irreversible"
    assert classify("click", "Close the savings account") == "irreversible"
    assert classify("click", "Close the dialog and view account") == "risky"


def test_the_order_is_total_and_irreversible_is_highest() -> None:
    assert RISK_ORDER["safe"] < RISK_ORDER["risky"] < RISK_ORDER["irreversible"]
    assert set(RISK_ORDER) == {"safe", "risky", "irreversible"}
