"""Spec §6.5: shape-preserving redaction as defence in depth over every evidence write.

Two vocabularies, deliberately: credentials keep the `[REDACTED]` marker (phase 2/4 --
a password's shape is not something to preserve); PII-shaped values get a shape-preserving
mask so a mismatch stays debuggable (criterion 6).
"""
import json

from cua.policy.redact import RedactingWriter, mask_digits, mask_field, redact, redact_leaves
from cua.replay.result import mint_run_id


def test_an_ssn_keeps_its_shape_and_its_last_four_digits() -> None:
    # Criterion 6: the last four survive so a mismatch stays debuggable.
    assert redact("SSN 412-55-0198") == "SSN ***-**-0198"
    assert redact("SSN 412-55-0198").endswith("0198")


def test_a_card_number_keeps_its_separators_and_last_four() -> None:
    assert redact("card 4111 1111 1111 1111") == "card **** **** **** 1111"
    assert redact("card 4111-1111-1111-1111") == "card ****-****-****-1111"
    assert redact("card 4111111111111111") == "card ************1111"


def test_an_account_number_keeps_its_last_four_and_its_sub_account_suffix() -> None:
    assert redact("Acct 000100045512") == "Acct ********5512"
    assert redact("Savings 000100045512-01 4,218.60") == "Savings ********5512-01 4,218.60"


def test_short_numbers_and_money_are_untouched() -> None:
    # A member id, a balance, a year: none of these are the shapes §6.5 names.
    assert redact("Member 12345 Balance 4,218.60 since 2019") == (
        "Member 12345 Balance 4,218.60 since 2019"
    )


def test_a_run_id_survives_redaction() -> None:
    # D35 pinned the run-id shape against the criterion-1 scan; the same care here. The
    # fourteen-digit timestamp must never look account-shaped, or every evidence_ref
    # written to disk would point nowhere.
    run_id = mint_run_id()
    assert redact(run_id) == run_id
    assert redact(f"evidence/{run_id}") == f"evidence/{run_id}"
    assert redact("run-20260916120000-ab12") == "run-20260916120000-ab12"


def test_mask_digits_keeps_the_last_four_digits_and_every_non_digit() -> None:
    assert mask_digits("4218.60") == "**18.60"
    assert mask_digits("412-55-0198") == "***-**-0198"
    assert mask_digits("no digits") == "no digits"


def test_mask_digits_never_returns_a_short_value_unchanged() -> None:
    # Fewer digits than `keep`: mask them all rather than reveal the whole value.
    assert mask_digits("42") == "**"
    assert mask_digits("1234") == "****"
    assert mask_digits("12345") == "*2345"


def test_mask_field_masks_a_value_with_no_digits_too() -> None:
    # E16: a `redact` output is masked whatever it holds. Digits: the last four survive.
    # No digits: every alphanumeric goes, separators stay, so length and word shape survive.
    assert mask_field("4218.60") == "**18.60"
    assert mask_field("Dana Whitfield") == "**** *********"
    assert mask_field("dana.whitfield@example.test") == "****.*********@*******.****"
    assert mask_field("") == ""


def test_redact_leaves_walks_nested_structures_and_leaves_non_strings_alone() -> None:
    data = {"a": "SSN 412-55-0198", "b": ["000100045512", 7, None], "c": {"d": True}}
    assert redact_leaves(data) == {
        "a": "SSN ***-**-0198", "b": ["********5512", 7, None], "c": {"d": True},
    }


def test_the_writer_redacts_every_byte_it_writes(tmp_path) -> None:
    writer = RedactingWriter()
    target = tmp_path / "nested" / "out.txt"
    writer.put_text(target, "SSN 412-55-0198\n")
    writer.put_line(target, json.dumps({"acct": "000100045512"}))
    assert target.read_text() == 'SSN ***-**-0198\n{"acct": "********5512"}\n'
