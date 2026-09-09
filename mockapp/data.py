"""Synthetic data. Every value here is invented. No real person, account, or institution."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Account:
    kind: str
    number: str
    balance: str


@dataclass(frozen=True)
class Member:
    member_id: str
    name: str
    ssn: str
    account_number: str
    accounts: list[Account] = field(default_factory=list)
    restricted: bool = False


MEMBERS: dict[str, Member] = {
    "12345": Member(
        member_id="12345",
        name="Dana Whitfield",
        ssn="412-55-0198",
        account_number="000100045512",
        accounts=[
            Account("Savings", "000100045512-01", "4,218.60"),
            Account("Checking", "000100045512-02", "312.04"),
        ],
    ),
    "22222": Member(
        member_id="22222",
        name="Priya Raghunathan",
        ssn="331-08-7742",
        account_number="000100077431",
        accounts=[Account("Savings", "000100077431-01", "89,410.22")],
    ),
    "99999": Member(
        member_id="99999",
        name="Restricted Record",
        ssn="000-00-0000",
        account_number="000000000000",
        accounts=[],
        restricted=True,
    ),
}
