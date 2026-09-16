"""Spec §6.3's risk heuristic: action type plus the control's accessible name, biased
toward over-classification, confirmed by a human at approval time.

Stated as a heuristic, not dressed up as risk detection. The tiers (E7):

- `read` and `wait_for` are `safe` whatever the control is called -- observing cannot
  mutate.
- A `click` or `press_key` on a control whose name carries a commit verb (`post`, `delete`,
  `submit payment`, or `close` followed within two words by `account`) is `irreversible`.
- Any other action on a name matching §6.3's `transfer|delete|close|post|submit payment`
  is `risky` -- this is what flags "Transfer History", a read-only link, and that is the
  documented cost of erring toward the recoverable mistake.
- Everything else is `safe`.

The approval gate is the real control (§6.4); this ordering only decides who has to look.
"""

from __future__ import annotations

import re

from cua.artifact.models import Risk
from cua.surface.models import ActionKind

__all__ = ["RISK_ORDER", "classify"]

RISK_ORDER: dict[Risk, int] = {"safe": 0, "risky": 1, "irreversible": 2}

_RISKY_RE = re.compile(r"\b(?:transfer|delete|close|post|submit payment)\b", re.IGNORECASE)
_IRREVERSIBLE_RE = re.compile(
    r"\b(?:post|delete|submit payment)\b|\bclose\b(?:\s+\w+){0,2}\s+account\b", re.IGNORECASE
)
_OBSERVING: frozenset[str] = frozenset({"read", "wait_for"})
_FIRING: frozenset[str] = frozenset({"click", "press_key"})


def classify(action: ActionKind, accessible_name: str | None) -> Risk:
    """The heuristic's verdict for one step. Never raises; an unnamed control is `safe`
    unless its action alone says otherwise (it does not, today)."""
    if action in _OBSERVING:
        return "safe"
    name = accessible_name or ""
    if action in _FIRING and _IRREVERSIBLE_RE.search(name):
        return "irreversible"
    if _RISKY_RE.search(name):
        return "risky"
    return "safe"
