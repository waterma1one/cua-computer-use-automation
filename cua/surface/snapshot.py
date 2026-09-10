"""Parses a browser's accessibility-tree snapshot (Playwright `aria_snapshot()` YAML) into a
flat list of `cua.surface.models.Node` objects.

Pure Python plus `yaml` plus `cua.surface.models` -- no browser library is imported here,
ever. `tests/test_architecture.py` enforces the boundary for the whole system; this module is
one of the pieces it protects, and Task 4's `cua/surface/web.py` is the only file allowed to
know that Playwright exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml

from cua.surface.models import Node, NodeState, SurfaceSegment

# Credential-token vocabulary used to infer a node's `protected` state from its accessible
# name alone. Nothing in the aria-snapshot YAML marks a field as a password field -- the
# accessible name is the only signal available to a parser whose signature is
# (yaml_text, surface_path, start_index). Matched case-insensitively on word boundaries, not
# as a substring: a naive substring match would make "shipping" and "spinner" protected.
# Deliberately extensible -- add tokens here as new leaky labels turn up in real applications.
# The known failure mode this cannot catch is a password field given an unusual label that
# names none of these tokens (e.g. a custom "Secret Word" field spelled in a way that misses
# every token here); there is no signal in the snapshot that could catch that case instead.
PROTECTED_NAME_TOKENS = (
    "password",
    "passwd",
    "passcode",
    "pin",
    "secret",
    "token",
    "otp",
    "cvv",
)

_PROTECTED_NAME_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(token) for token in PROTECTED_NAME_TOKENS) + r")\b",
    re.IGNORECASE,
)

# One aria-snapshot entry key, e.g.:
#   textbox "Member ID"
#   row "Password teller-demo-pw"
#   textbox
#   button "Post Transfer" [disabled]
_ENTRY_RE = re.compile(
    r'^(?P<role>\S+)(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?(?P<attrs>(?:\s*\[[^\]]*\])*)\s*$'
)
_ATTR_RE = re.compile(r"\[([^\]]*)\]")


@dataclass
class _Entry:
    """One flattened snapshot line, before `protected` inference and ancestor scrubbing."""

    role: str
    name: str | None
    value: str | None
    depth: int
    disabled: bool
    checked: bool
    expanded: bool


def _is_protected(name: str | None) -> bool:
    if not name:
        return False
    return _PROTECTED_NAME_RE.search(name) is not None


def _parse_key(key: str) -> tuple[str, str | None, bool, bool, bool]:
    match = _ENTRY_RE.match(key.strip())
    if not match:
        # Total parser: an unrecognized line becomes an `unknown` node rather than raising.
        return "unknown", None, False, False, False

    role = match.group("role")
    name = match.group("name")
    if name is not None:
        name = name.replace('\\"', '"')

    disabled = checked = expanded = False
    for attr_group in _ATTR_RE.finditer(match.group("attrs") or ""):
        for token in attr_group.group(1).split(","):
            attr_key = token.split("=")[0].strip().lower()
            if attr_key == "disabled":
                disabled = True
            elif attr_key == "checked":
                checked = True
            elif attr_key == "expanded":
                expanded = True

    return role, name, disabled, checked, expanded


def _iter_container(container: Any) -> list[tuple[Any, Any]]:
    """Normalizes one level of the parsed YAML into (key, value) pairs.

    A sequence item is either a bare scalar (a leaf with no value of its own, e.g.
    `- cell "Member Search"`) or a single-key mapping (`- row "...":` with a list or scalar
    value). A top-level mapping (`yaml.safe_load` on `'textbox "PIN": hunter2\\n'`, which is
    not list-shaped YAML) is just as valid an input and is handled the same way.
    """
    if container is None:
        return []
    if isinstance(container, list):
        pairs: list[tuple[Any, Any]] = []
        for item in container:
            if isinstance(item, dict):
                pairs.extend(item.items())
            else:
                pairs.append((item, None))
        return pairs
    if isinstance(container, dict):
        return list(container.items())
    return [(container, None)]


def _walk(container: Any, depth: int, out: list[_Entry]) -> None:
    for key, value in _iter_container(container):
        key_text = str(key)
        if key_text.startswith("/"):
            # R13: `/url`, `/checked`, etc. are node properties, not nodes. Skipping them
            # entirely (rather than emitting an `unknown` node) is the one exception to the
            # totality rule, because a URL is the one way DOM content could otherwise sneak
            # into an Observation that is specified to contain none.
            continue

        role, name, disabled, checked, expanded = _parse_key(key_text)

        if isinstance(value, (list, dict)):
            out.append(_Entry(role, name, None, depth, disabled, checked, expanded))
            _walk(value, depth + 1, out)
        else:
            leaf_value = None if value is None else str(value)
            out.append(_Entry(role, name, leaf_value, depth, disabled, checked, expanded))


def _scrub_text(name: str, secret: str) -> str:
    scrubbed = name.replace(secret, "")
    return re.sub(r"\s+", " ", scrubbed).strip()


def _scrub_ancestor_names(entries: list[_Entry]) -> None:
    """R12: a protected value leaks through the accessible names of its ancestors, not just
    its own value. Ancestors are identified through the `depth` chain -- walking backward from
    the protected entry and taking, in order, each preceding entry whose depth is strictly
    less than the shallowest ancestor depth found so far. That skips sibling subtrees (a
    preceding entry at the same or greater depth) without breaking the walk, which is what
    lets it reach a grandparent past an intervening sibling cell.

    Ancestor-scoped, not a global sweep: only entries identified as ancestors of a specific
    protected entry are touched, so an unrelated two-character password cannot blank out
    unrelated text elsewhere on the page.
    """
    for i, entry in enumerate(entries):
        if not (_is_protected(entry.name) and entry.value):
            continue
        secret = entry.value
        current_min_depth = entry.depth
        for other in reversed(entries[:i]):
            if other.depth < current_min_depth:
                if other.name and secret in other.name:
                    other.name = _scrub_text(other.name, secret)
                current_min_depth = other.depth
                if current_min_depth <= 0:
                    break


def parse_aria_snapshot(
    yaml_text: str,
    surface_path: list[SurfaceSegment],
    start_index: int = 0,
) -> list[Node]:
    """Flattens one frame's accessibility-tree snapshot into indexed, protection-aware nodes.

    Indices are dense, zero-based, and offset by `start_index` so that a caller concatenating
    several frames' nodes into one Observation can keep indices continuous across frames.
    Every returned node carries `surface_path` unmodified and `depth` set to its nesting level
    in the snapshot (0 for a top-level entry).

    `yaml.safe_load` is deliberately unguarded here: a `yaml.YAMLError` from malformed input
    is allowed to propagate rather than being caught and turned into an empty node list.
    Totality is about tolerating unrecognized *entries* within otherwise well-formed YAML (an
    unfamiliar line becomes an `unknown`-role node rather than raising), not about tolerating
    broken YAML syntax. `aria_snapshot()` always emits well-formed YAML, so malformed input
    here means something upstream is plumbed wrong, and a loud failure is the correct
    response: silently degrading to an empty `Observation` would read to the discovery loop as
    "this page has no controls," which is a worse failure than a crash.
    """
    data = yaml.safe_load(yaml_text)
    entries: list[_Entry] = []
    _walk(data, 0, entries)
    _scrub_ancestor_names(entries)

    nodes: list[Node] = []
    for offset, entry in enumerate(entries):
        protected = _is_protected(entry.name)
        value = None if protected else entry.value
        state = NodeState(
            disabled=entry.disabled,
            checked=entry.checked,
            expanded=entry.expanded,
            protected=protected,
        )
        nodes.append(
            Node(
                index=start_index + offset,
                role=entry.role,
                name=entry.name or None,
                value=value,
                state=state,
                surface_path=surface_path,
                depth=entry.depth,
            )
        )
    return nodes


def scrub_protected_values(yaml_text: str) -> str:
    """Returns the raw snapshot YAML with every protected node's value removed globally.

    For spec §3.7.2: the raw snapshot is written to an evidence directory in a later phase,
    and a password is not SSN-shaped or card-shaped, so the shape-based redaction that phase
    adds is blind to it. Unlike `parse_aria_snapshot`'s ancestor-scoped scrub, this scrubs the
    value wherever it appears in the text -- it is written to disk for a human to read and is
    never matched against, so there is no risk of an unrelated match elsewhere on the page
    being blanked out by mistake in a way that matters.
    """
    data = yaml.safe_load(yaml_text)
    entries: list[_Entry] = []
    _walk(data, 0, entries)

    secrets = {entry.value for entry in entries if entry.value and _is_protected(entry.name)}

    scrubbed = yaml_text
    for secret in secrets:
        scrubbed = scrubbed.replace(secret, "")
    return scrubbed
