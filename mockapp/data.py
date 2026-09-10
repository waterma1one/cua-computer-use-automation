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


NOT_FOUND_MESSAGE = "No member found"
RESTRICTED_MESSAGE = "You are not authorized to view this record"


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


def resolve_member(member_id: str) -> Member | str:
    """The single place that decides whether a member record may be shown.

    Every route that accepts a member_id calls this instead of reading MEMBERS
    directly, so "not found" and "restricted" stay business outcomes owned by the
    record itself rather than something each route re-derives (or forgets to).

    Returns the Member when it may be displayed, or one of NOT_FOUND_MESSAGE /
    RESTRICTED_MESSAGE when it may not.
    """
    member = MEMBERS.get(member_id)
    if member is None:
        return NOT_FOUND_MESSAGE
    if member.restricted:
        return RESTRICTED_MESSAGE
    return member


def resolve_account(number: str) -> tuple[Member, Account] | str:
    """Resolve an account number to its owning member and account record.

    Scans every member's accounts for a matching number, then hands the owning member's
    id to `resolve_member` rather than returning the member found in this loop directly --
    that keeps visibility a single decision owned by `resolve_member`, not something this
    lookup re-derives. Returns the `(Member, Account)` pair when it may be shown, or
    NOT_FOUND_MESSAGE / RESTRICTED_MESSAGE when it may not (identically to `resolve_member`,
    an account belonging to no visible member is indistinguishable from one that does not
    exist at all).
    """
    for member in MEMBERS.values():
        for account in member.accounts:
            if account.number == number:
                resolved = resolve_member(member.member_id)
                if isinstance(resolved, str):
                    return resolved
                return resolved, account
    return NOT_FOUND_MESSAGE
