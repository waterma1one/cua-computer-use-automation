"""Shape-preserving redaction: spec §6.5's pattern filter, applied to every evidence write.

Two vocabularies live side by side and must not be confused:

- A **credential** (a password, a token) is scrubbed to the literal `[REDACTED]` by
  `cua.surface.snapshot.scrub_protected_values` and by the sensitive-input masking in
  `cua.observability` and `cua.cli`. Nothing about a credential's shape is worth keeping.
- A **PII-shaped value** (an SSN, a card number, an account number) is masked here with its
  shape intact and its last four digits kept, because a redaction that destroys shape
  destroys the ability to debug a mismatch (§6.5).

`RedactingWriter` is the one class that writes evidence text to disk. `RunLog` and
`EvidenceWriter` go through it; `tests/test_architecture.py` greps them for any direct write.
Screenshots are pixels and are not redacted -- a stated limit, not an oversight.

Bounds, stated: the account shape is ten to twelve digits so that a fourteen-digit run-id
timestamp (`run-YYYYMMDDHHMMSS-xxxx`, `cua.replay.result.mint_run_id`) can never match --
an `evidence_ref` written to disk must keep pointing where it points. A bare thirteen-to-
seventeen-digit account number is therefore not caught by this filter.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["RedactingWriter", "mask_digits", "mask_field", "redact", "redact_leaves"]

_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD_RE = re.compile(r"\b\d{4}([ -]?)\d{4}\1\d{4}\1\d{4}\b")
_ACCOUNT_RE = re.compile(r"\b(\d{10,12})(-\d{2})?\b")


def mask_digits(value: str, *, keep: int = 4) -> str:
    """Replaces every digit in `value` with `*` except the last `keep` digits; every
    non-digit character stays where it is, so the shape survives. A value with `keep` or
    fewer digits has every digit masked -- "keep the last four" must never mean "reveal
    the whole thing".
    """
    digits = sum(ch.isdigit() for ch in value)
    to_mask = digits if digits <= keep else digits - keep
    out: list[str] = []
    seen = 0
    for ch in value:
        if ch.isdigit():
            seen += 1
            out.append("*" if seen <= to_mask else ch)
        else:
            out.append(ch)
    return "".join(out)


def mask_field(value: str) -> str:
    """The field-level `redact` mask (spec §6.5, E16): `mask_digits` for a value carrying
    digits; otherwise every alphanumeric character becomes `*` and separators stay, so a
    name or an address keeps its length and word shape and nothing else."""
    if any(ch.isdigit() for ch in value):
        return mask_digits(value)
    return "".join("*" if ch.isalnum() else ch for ch in value)


def _mask_account(match: re.Match[str]) -> str:
    # The `-NN` suffix is a sub-account discriminator, not the secret; the digit run is.
    return mask_digits(match.group(1)) + (match.group(2) or "")


def redact(text: str) -> str:
    """The pattern filter: SSN-shaped, card-shaped and account-shaped values in `text` are
    masked with their shape intact. Order matters only in that an SSN's hyphenated groups
    must be masked as an SSN before the account pattern could see any of its digits.
    """
    text = _SSN_RE.sub(lambda m: mask_digits(m.group(0)), text)
    text = _CARD_RE.sub(lambda m: mask_digits(m.group(0)), text)
    return _ACCOUNT_RE.sub(_mask_account, text)


def redact_leaves(value: object) -> object:
    """`redact` applied to every string leaf of a JSON-shaped structure; dict keys, numbers,
    booleans and `None` pass through unchanged."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_leaves(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_leaves(item) for item in value]
    return value


class RedactingWriter:
    """Writes text to disk with `redact` applied to every byte. Parent directories are
    created. The one place evidence text reaches a file, so the pattern filter cannot be
    bypassed by forgetting to call it.
    """

    def put_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(redact(text))

    def put_line(self, path: Path, line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(redact(line))
            handle.write("\n")
