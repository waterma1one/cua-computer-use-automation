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

from cua.surface.models import (
    Node,
    NodeState,
    SurfaceSegment,
    ancestor_positions,
    is_protected_name,
)

# S3: the marker `scrub_protected_values` substitutes for a secret in the raw evidence YAML.
# A visible marker, rather than the empty string the original implementation used, means a
# human reading /evidence/ can tell scrubbing happened -- an empty-string replace is
# indistinguishable from a field that was simply blank, and silently corrupts unrelated text
# that happens to share the secret's substring (see `scrub_protected_values`'s docstring).
_REDACTION_MARKER = "[REDACTED]"

# E6: the credential-name rule -- vocabulary *and* matcher -- now lives in
# `cua.surface.models.is_protected_name` (imported above), because `cua.artifact.validate`
# needs the same rule for spec §4.4's sixth condition and two byte-identical copies of a
# security-relevant rule drift. A drift here is a credential leaking past one of the two
# checks that exist to stop it.
#
# Nothing in the aria-snapshot YAML marks a field as a password field, so the accessible
# name remains the only signal available to a parser whose signature is
# (yaml_text, surface_path, start_index). See that predicate's docstring for the three blind
# spots that follow from inferring protection from a name, and tests/surface/test_snapshot.py
# for the ones pinned from this side.

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


class _StringPreservingLoader(yaml.SafeLoader):
    """A `SafeLoader` that hands back every scalar as the literal text it appeared as in the
    source, never re-typed to `int`/`float`/`bool`/`None`.

    S1: `yaml.safe_load` on a bare `0755` returns the Python int 493 (YAML 1.1's octal
    resolver), and `str(493)` is `"493"` -- not `"0755"`. The same silent bypass happens for
    `no`/`on`/`yes`/`off` (parsed as `bool`) and `null`/`~` (parsed as `None`), and partially
    for `1.50` (`str(1.5)` drops the trailing zero). If any of these is a protected value's
    literal text, scrubbing that re-stringifies the *parsed* scalar instead of using the
    *source* text misses it, or matches the wrong substring -- and the secret survives
    verbatim in an ancestor's name or in the evidence YAML. Every scalar constructor that
    would otherwise re-type a plain scalar is overridden below to return `node.value`, the
    raw source text, unchanged.
    """


def _construct_as_written(_loader: yaml.SafeLoader, node: yaml.ScalarNode) -> str:
    return str(node.value)


def _construct_null_as_written(_loader: yaml.SafeLoader, node: yaml.ScalarNode) -> str | None:
    # An explicit null-ish word (`null`, `~`, ...) has non-empty source text and must be
    # preserved as that literal string so it can be scrubbed like any other scalar. A truly
    # elided value (`key:` with nothing after the colon) has empty source text; that case is
    # a structural "no value" leaf, not a scalar to preserve, so it stays `None`.
    text = str(node.value)
    return text if text else None


for _tag in (
    "tag:yaml.org,2002:bool",
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:float",
    "tag:yaml.org,2002:timestamp",
):
    _StringPreservingLoader.add_constructor(_tag, _construct_as_written)
_StringPreservingLoader.add_constructor("tag:yaml.org,2002:null", _construct_null_as_written)


def _load_yaml(yaml_text: str) -> Any:
    """Parses `yaml_text` with every scalar preserved as its literal source text (S1).

    Deliberately unguarded, like the `yaml.safe_load` it replaces: a `yaml.YAMLError` from
    malformed input is allowed to propagate rather than being caught and turned into an
    empty node list. See `parse_aria_snapshot`'s docstring for why a loud failure is correct
    here.
    """
    return yaml.load(yaml_text, Loader=_StringPreservingLoader)


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
    """R12/S2: a protected value leaks through its own entry's name and through the
    accessible names of its ancestors, not just its own value. Ancestors are identified
    through the shared `cua.surface.models.ancestor_positions` walk (R22) -- the same one
    `locators.ancestors_of` uses -- rather than a second, separately-maintained copy.

    Ancestor-scoped, not a global sweep: only entries identified as ancestors of a specific
    protected entry are touched, so an unrelated two-character password cannot blank out
    unrelated text elsewhere on the page.
    """
    depths = [e.depth for e in entries]
    for i, entry in enumerate(entries):
        if not (is_protected_name(entry.name) and entry.value):
            continue
        secret = entry.value
        # S2: the protected entry's own name is scrubbed too -- a hostile but real label
        # shape (`textbox "Password hunter2"`) leaks the value right there, and only the
        # ancestors were ever touched before.
        if entry.name and secret in entry.name:
            entry.name = _scrub_text(entry.name, secret)
        for j in ancestor_positions(i, depths):
            other = entries[j]
            if other.name and secret in other.name:
                other.name = _scrub_text(other.name, secret)


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

    Parsing (`_load_yaml`, S1) is deliberately unguarded here: a `yaml.YAMLError` from
    malformed input is allowed to propagate rather than being caught and turned into an
    empty node list. Totality is about tolerating unrecognized *entries* within otherwise
    well-formed YAML (an unfamiliar line becomes an `unknown`-role node rather than raising),
    not about tolerating broken YAML syntax. `aria_snapshot()` always emits well-formed YAML,
    so malformed input here means something upstream is plumbed wrong, and a loud failure is
    the correct response: silently degrading to an empty `Observation` would read to the
    discovery loop as "this page has no controls," which is a worse failure than a crash.
    """
    data = _load_yaml(yaml_text)
    entries: list[_Entry] = []
    _walk(data, 0, entries)
    _scrub_ancestor_names(entries)

    nodes: list[Node] = []
    for offset, entry in enumerate(entries):
        protected = is_protected_name(entry.name)
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
    """Returns the raw snapshot YAML with every protected node's value replaced by a visible
    redaction marker, globally.

    For spec §3.7.2: the raw snapshot is written to an evidence directory in a later phase,
    and a password is not SSN-shaped or card-shaped, so the shape-based redaction that phase
    adds is blind to it. Unlike `parse_aria_snapshot`'s ancestor-scoped scrub, this scrubs the
    value wherever it appears in the text, because it is written to disk for a human to read
    and is never matched against programmatically.

    S3: that global replace is a real risk, not a cosmetic one, if the substitution is the
    empty string -- a legitimate `button "Search"` silently becomes `button ""` when the
    password happens to be "Search", and a one-character password (`"e"`) mangles role names
    outright (`textbox` -> `txtbox`). Substituting a visible marker instead does not prevent
    an unrelated match from being touched -- a global textual replace has no way to tell "the
    secret" from "unrelated text that happens to contain the same substring" -- but it does
    mean the touched-but-unrelated text now reads as visibly redacted rather than silently
    blanked or corrupted into a different, plausible-looking word.
    """
    data = _load_yaml(yaml_text)
    entries: list[_Entry] = []
    _walk(data, 0, entries)

    secrets = {entry.value for entry in entries if entry.value and is_protected_name(entry.name)}

    scrubbed = yaml_text
    for secret in secrets:
        scrubbed = scrubbed.replace(secret, _REDACTION_MARKER)
    return scrubbed
