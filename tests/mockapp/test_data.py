import re

from mockapp.data import MEMBERS, Member


def test_members_exist() -> None:
    assert "12345" in MEMBERS
    assert isinstance(MEMBERS["12345"], Member)


def test_member_carries_regulated_looking_fields() -> None:
    m = MEMBERS["12345"]
    assert re.fullmatch(r"\d{3}-\d{2}-\d{4}", m.ssn), "redaction needs an SSN-shaped value"
    assert re.fullmatch(r"\d{10,12}", m.account_number)
    assert any(a.kind == "Savings" for a in m.accounts)
    assert any(a.kind == "Checking" for a in m.accounts)


def test_a_member_exists_that_the_teller_may_not_view() -> None:
    restricted = [k for k, v in MEMBERS.items() if v.restricted]
    assert restricted, "permission-denied needs a member to deny access to"
