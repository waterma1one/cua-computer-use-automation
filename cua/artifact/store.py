"""The store: where the immutable artifact file and the mutable registry part company.

This is the only module in `cua/artifact/` that touches the filesystem. Everything else
in the package (`models.py`, `validate.py`, `overlay.py`, `schema.py`) is pure data
shaping; here that data is actually written to and read back from disk.

Spec §4.1's closing paragraph draws the line this module enforces: `status`
(`draft | approved`) and `stability` (`replays`, `successes`, `score`) are deliberately
**not** on `Artifact` (see that model's own docstring) and never reach
`artifacts/<id>/v<version>.yaml`. They live in `artifacts/registry.json`, keyed by
`(id, version)`, which this module reads and writes as `RegistryEntry`. The artifact file
itself is immutable once written: `save` refuses to overwrite an existing version, exactly
as §4.1 states ("a new revision is a new version number").

**How `load` surfaces findings, and why.** Task 2's handoff for this task read: "`validate`
is the load-time gate -- call it on read, and surface warnings and notes to the operator
rather than dropping them." Two things follow from that, and they are different things:

1. An `error`-level finding means the artifact must not be treated as replayable at all --
   `load` raises `ValueError` rather than handing back an `Artifact` that looks fine but
   is not.
2. A `warning` or `note` is not a defect that should stop a load (E4's whole point is that
   "not checked" must never be silently swallowed, but it is also not "invalid" -- an
   artifact with no `policy` block is still perfectly loadable). Dropping these on the
   floor would defeat E4 exactly as reading past it would. So every finding, at every
   level, is logged through this module's logger, and (ruling E17) the full list is
   **always** part of `load`'s return value -- there is no opt-in that a caller could
   forget to pass. `load` returns `(artifact, findings)` unconditionally. An earlier
   revision of this module gated the findings list behind `return_findings=True`; that
   opt-in is exactly the failure E4 exists to prevent (a bare call site looks fully
   checked when it is not) and also forced a `mypy --strict`-hostile `Artifact |
   tuple[Artifact, list[Finding]]` union onto every caller, so it is gone.

This module does not decide whether a `draft` artifact may be exported as a callable tool
-- ruling E12, carried from Task 4's handoff, puts that three-part gate (`validate`
clean, `artifact.verified`, registry `status != draft`) on whoever calls
`export_tool_schema`, not on this module or that one. What this module *does* gate is
narrower and different, and (rulings E18, E19) now has three parts instead of one:

1. `write_registry_entry` refuses to set `status="approved"` on an artifact that still
   carries an unverified `Expect` (acceptance criterion 6, §8.3 step 5) -- a promotion-time
   check about the artifact's own admitted state.
2. `write_registry_entry` refuses `requires_human_approval=False` wherever it cannot prove
   the flag clearable: on an artifact holding any step classified `risk="irreversible"`
   (§6.4, ruling E15), and -- ruling E15's other clause -- when no artifact can be
   resolved at all, because "nothing to check against" is not proof of anything. The flag
   defaults `True` and may be *cleared* only where §6.4 gives no rule against it AND an
   artifact was actually available to check.
3. `write_registry_entry` refuses `status="approved"` if the resolved artifact fails
   load-time validation (any `error`-level finding from `validate()`) -- ruling E19. This
   is deliberately **not** required for `status="draft"`: a fresh discovery run's file, or
   an artifact still being iterated on, must be registrable as `draft` whether or not it
   would pass `validate()` yet. Only promotion to `approved` demands that.
4. All three of the above need an artifact to inspect, and treat "resolving" and
   "validating" as two different steps (ruling E19) -- resolving an on-disk file for the
   identity/E15/expect checks never runs `validate()`'s full gate (see `_parse_artifact`),
   so a `draft` registration never depends on that gate passing; only the `approved` check
   above explicitly re-invokes it. `write_registry_entry` no longer trusts an `artifact`
   argument blindly either: passing one that does not itself claim this `(id, version)` is
   refused rather than silently trusted. See `write_registry_entry`'s own docstring for the
   exact resolution order (ruling E18) and why: an artifact argument naming a different
   `(id, version)` than the call, an `approved` entry for an `(id, version)` this store
   holds no file for at all, and a `requires_human_approval=False` entry with nothing to
   check it against, were all silent holes in the original gate, closed here.

**Criterion 1 is now enforced at `save`, not only pinned by a fixture-shaped test.**
Ruling E16: a hostname, an IP address, a URL scheme, a CSS selector, an XPath, or a
credential literal written into a protected control must never reach the artifact file,
whichever field it arrives through -- including one a later phase adds without anyone
remembering to extend a test fixture. See `_forbidden_content_violations`'s docstring for
the detector's shape and its documented blind spots.

**On E11 and `app.variant`: this store keys and paths by `(id, version)` alone, never by
variant, and that is a conscious choice, not an oversight.** `resolve_overlay` (Task 3)
does not change `Artifact.id` or `Artifact.version` -- only `app.variant`, `steps`, and
`verified` -- so a resolved tenant artifact carries the *same* `(id, version)` as the base
it was resolved from. S4.3 calls a tenant variant "an overlay file resolved onto the base
artifact at load time", which this module reads as: only the base artifact is ever
persisted through `save`/`load`; a resolved variant is a transient value `resolve_overlay`
hands back in memory and is never itself passed to `save` here. Nothing in this task's
interface takes an `Overlay` or a variant, so there is no call site today that would even
attempt to save a resolved artifact under its base's `(id, version)` and collide with it.
If a later phase decides to cache a resolved variant to disk, `(id, version)` alone is not
a safe key for that -- it would silently collide with the base and with every other
variant resolved from it -- and that phase must extend the key, not this one.

Like the rest of `cua/artifact/`, nothing here imports Playwright, references a DOM, or
carries a CSS selector or an XPath.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from cua.artifact.models import Artifact
from cua.artifact.validate import Finding, _literal_findings, validate

logger = logging.getLogger(__name__)


class RegistryEntry(BaseModel):
    """One `(id, version)`'s lifecycle state, as stored in `artifacts/registry.json`.

    `status` is exactly S4.1's `draft | approved`. `replays`, `successes`, and `score` are
    S4.1's `stability` group, flattened onto this model rather than nested under a
    `stability` field of their own -- there is nothing else in `stability` for a nested
    model to hold, and flattening keeps `RegistryEntry(status=..., replays=..., ...)`
    constructible with plain keyword arguments rather than a nested mapping literal.

    `requires_human_approval` defaults to `True`: §6.3's stated asymmetry is that
    over-classification costs a human a moment, while an irreversible action executed
    unattended cannot be undone, so an entry starts out requiring a human and must be
    affirmatively cleared, rather than starting permissive and requiring someone to
    remember to lock it down. Ruling E15 makes half of that asymmetry a hard rule rather
    than only a default: `write_registry_entry` refuses to clear this flag to `False` on
    an artifact holding any `risk="irreversible"` step (§6.4 gives a rule there; the
    default alone is not enough). Where §6.4 gives no rule -- every other artifact -- the
    field stays a plain default a caller may clear, and computing a *tighter* value than
    `True` from an artifact's full risk classification is still a later phase's job.

    `extra="forbid"` (E5): a mistyped or misplaced key here should fail loudly, since a
    human is the one filling this out via the operator console (later phases) or by hand.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["draft", "approved"] = "draft"
    replays: int = 0
    successes: int = 0
    score: float | None = None
    requires_human_approval: bool = True


def _artifact_dir(root: Path, artifact_id: str) -> Path:
    return root / "artifacts" / artifact_id


def _artifact_path(root: Path, artifact_id: str, version: int) -> Path:
    return _artifact_dir(root, artifact_id) / f"v{version}.yaml"


def _registry_path(root: Path) -> Path:
    return root / "artifacts" / "registry.json"


def _log_findings(findings: list[Finding], *, artifact_id: str, version: int) -> None:
    """Surfaces every finding through the module logger, whatever its level.

    Deliberately unconditional: an `error` is logged here too, immediately before `load`
    raises over it, so the log carries the full picture rather than just the one finding
    that happened to trip the raise.
    """
    for finding in findings:
        logger.warning(
            "%s v%s: [%s] %s%s -- %s",
            artifact_id, version, finding.level, finding.code,
            f" ({finding.where})" if finding.where else "",
            finding.message,
        )


# --- Acceptance criterion 1, enforced at the one chokepoint every byte crosses (E16) ------
#
# The test-level scan in `tests/artifact/test_store.py` pins this against `factories.base()`
# alone: it can only ever catch a forbidden value that some fixture happens to carry. The
# checks below run on whatever `save` is actually about to write, so a field a later phase
# adds is covered the day it is added, with nobody needing to remember to extend a test.
#
# Credentials reuse `cua.artifact.validate._literal_findings` -- the same §4.4 sixth-
# condition rule `load` already enforces -- rather than a second, drifting copy (E6/R22:
# one security-relevant rule, one implementation). The remaining legs (hostname, IP
# address, URL scheme, CSS selector, XPath) have no existing implementation to reuse; they
# are pattern-based and their blind spots are documented on `_forbidden_content_violations`
# itself, in the style `is_protected_name` uses.

# A scheme prefix: "http://", "https://", "ws://", "ftp://", etc. Requires "://" so a
# locator's `role_name` strategy or an ordinary sentence ending in a colon never matches.
_URL_SCHEME_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]{1,15}://")

# An IPv4 address, with an optional ":port". Four dot-separated 1-3 digit groups is
# structural enough that it will not fire on a version string ("2.5") or a decimal id.
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b(?::\d{1,5})?")

# A bare hostname: a label immediately followed by one of a curated set of TLD-shaped
# suffixes. The suffix list, not a bare "label.label" pattern, is what keeps this from
# tripping on `Artifact.id` values like "corebank.probe" -- "probe" is not a TLD. Blind
# spot, documented rather than silently assumed away: a real host under an unlisted
# suffix (".xyz", ".ai", a corporate TLD not in this list) is not caught. Executed:
# `_HOSTNAME_SUFFIX_RE.search("corebank.probe")` is `None`;
# `_HOSTNAME_SUFFIX_RE.search("acme.corebank.internal")` matches `"corebank.internal"`.
_HOSTNAME_SUFFIX_RE = re.compile(
    r"\b[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\."
    r"(?:com|net|org|io|dev|app|co|biz|info|gov|edu|internal|local|corp|lan|test|example)\b",
    re.IGNORECASE,
)
_LOCALHOST_RE = re.compile(r"\blocalhost\b", re.IGNORECASE)

# A CSS-selector-shaped token: a "." or "#" that starts the string or follows whitespace,
# immediately followed by an identifier character. Anchoring on "start-of-token" is what
# keeps this from matching the "." inside "corebank.probe" or "gemini-2.5-flash-lite" --
# those dots are preceded by a letter or digit, never by whitespace or the start of the
# field. Blind spot: a selector embedded mid-word with no separating space ("seeclass.btn")
# is not caught, and a combinator-only selector ("div > span") with no leading "." or "#"
# is not caught either -- both documented rather than assumed covered. Executed:
# `_CSS_SELECTOR_RE.search("corebank.probe")` is `None`;
# `_CSS_SELECTOR_RE.search(".btn-primary")` matches; `_CSS_SELECTOR_RE.search("a #submit")`
# matches.
_CSS_SELECTOR_RE = re.compile(r"(?:^|\s)[.#][A-Za-z_][\w-]*")

# An XPath-shaped token: "//" starting the string or following whitespace and immediately
# followed by a tag/role character, an attribute predicate "[@...", or an axis "::". A
# single leading "/" is deliberately NOT enough -- `App.entry` and `Target.path` are
# legitimate single-slash application paths (e.g. "/teller/index.html") and must save.
# Blind spot: a relative XPath with no leading slash at all ("button[@id='x']") is not
# caught by the "//" leg, though it is still caught by "[@". Executed:
# `_XPATH_RE.search("/teller/index.html")` is `None`;
# `_XPATH_RE.search("//button[@id='x']")` matches.
_XPATH_RE = re.compile(r"(?:^|\s)//[A-Za-z@*]|\[@[A-Za-z]|::[A-Za-z]")

# Locator-string-style substrings a Playwright/Selenium-flavored locator would carry.
# Blunt on purpose, as a backstop for exactly the syntax the two regexes above do not
# structurally model.
_LOCATOR_SYNTAX_SUBSTRINGS = ("css=", "xpath", "queryselector", "nth-child")


def _iter_strings(value: Any) -> Iterator[str]:
    """Every string leaf reachable from `value` (a `model_dump(mode="json")` tree).

    Walks dict values and list items only -- dict *keys* (e.g. an output's declared name)
    are never yielded, because a key is an identifier the schema itself constrains, not
    free text a discovery run could have injected a hostname or selector into.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _iter_strings(v)


def _pattern_violations(text: str) -> list[str]:
    hits: list[str] = []
    if _URL_SCHEME_RE.search(text):
        hits.append(f"a URL scheme in {text!r}")
    if _IP_RE.search(text):
        hits.append(f"an IP address in {text!r}")
    if _HOSTNAME_SUFFIX_RE.search(text) or _LOCALHOST_RE.search(text):
        hits.append(f"a hostname in {text!r}")
    if _CSS_SELECTOR_RE.search(text):
        hits.append(f"a CSS selector in {text!r}")
    if _XPATH_RE.search(text):
        hits.append(f"an XPath in {text!r}")
    lowered = text.lower()
    for substring in _LOCATOR_SYNTAX_SUBSTRINGS:
        if substring in lowered:
            hits.append(f"locator syntax ({substring!r}) in {text!r}")
    return hits


def _where(index: int, artifact: Artifact) -> str:
    return f"steps[{index}] ({artifact.steps[index].id})"


def _forbidden_content_violations(artifact: Artifact, data: dict[str, Any]) -> list[str]:
    """Acceptance criterion 1, run on the exact tree `save` is about to write.

    Two independent passes:

    1. Credentials: `cua.artifact.validate._literal_findings` -- the same §4.4 sixth-
       condition check `load` runs -- applied per step. This is intentionally the *same*
       function, not a re-implementation, per E6/R22 and ruling E16. It is why a `Locator`
       named "Password" filled from an *input* (M1's mock-app scenario) saves cleanly:
       the rule fires only when a `LiteralValue` is written into a protected-named
       control, never merely because a control happens to be named one.
    2. Network/locator-syntax patterns: `_pattern_violations`, applied to every string leaf
       of the serialized tree (`_iter_strings`), because a hostname or selector can arrive
       through any field, including one a later phase adds.

    Returns human-readable violation strings; an empty list means `save` proceeds.
    """
    violations: list[str] = []
    for index, step in enumerate(artifact.steps):
        for finding in _literal_findings(artifact, step, _where(index, artifact)):
            violations.append(finding.message)
    for text in _iter_strings(data):
        violations.extend(_pattern_violations(text))
    return violations


def save(artifact: Artifact, root: Path) -> Path:
    """Writes `artifact` to `artifacts/<id>/v<version>.yaml` under `root`, immutably.

    Refuses (`FileExistsError`) to overwrite an existing version file -- §4.1: the
    artifact file is immutable, and a new revision is a new version number, never an
    in-place edit. Refuses (`ValueError`, ruling E16) to write an artifact whose serialized
    content carries a hostname, an IP address, a URL scheme, a CSS selector, an XPath, or a
    credential literal written into a protected control -- see
    `_forbidden_content_violations` for the detector and its documented blind spots. Both
    checks run before any directory is created or any byte written, so a refused save
    leaves the store untouched.

    Written with `mode="json"` (so `datetime`, and every nested Pydantic model, becomes
    plain YAML-representable data) and `sort_keys=False`, so the file reads in the field
    order §4.1's shape declares -- a human reviews this file, and `schema_version` before
    `id` before `version` is the order the spec itself writes them in, which alphabetical
    sorting would scramble.
    """
    path = _artifact_path(root, artifact.id, artifact.version)
    if path.exists():
        raise FileExistsError(
            f"{path} already exists; artifacts/<id>/v<version>.yaml is immutable -- "
            f"write a new version instead of overwriting this one"
        )
    data: dict[str, Any] = artifact.model_dump(mode="json", exclude_none=False)
    violations = _forbidden_content_violations(artifact, data)
    if violations:
        raise ValueError(
            f"refusing to save {artifact.id} v{artifact.version}: the serialized artifact "
            f"would carry content acceptance criterion 1 forbids: "
            + "; ".join(violations)
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
    return path


def _parse_artifact(path: Path) -> Artifact:
    """Reads and parses one artifact file's YAML into an `Artifact` -- structural parsing
    only, no `validate()` call.

    Factored out of `load` (ruling E19) so `write_registry_entry` can resolve an on-disk
    file for its identity/expect/irreversible-step checks without going through `load`'s
    load-time-validation gate: a `draft` registration must never depend on the artifact
    passing `validate()` -- only a promotion to `approved` does, and that gate calls
    `validate()` explicitly for itself (see `write_registry_entry`).
    """
    data = yaml.safe_load(path.read_text())
    return Artifact.model_validate(data)


def _load_time_errors(artifact: Artifact) -> list[Finding]:
    """`validate()`'s `error`-level findings only -- the same rule `load` raises over,
    reused (not re-implemented) by `write_registry_entry`'s approval gate (ruling E19) so
    "what counts as invalid" has exactly one definition.
    """
    return [f for f in validate(artifact) if f.level == "error"]


def load(id: str, version: int, root: Path) -> tuple[Artifact, list[Finding]]:
    """Reads back `artifacts/<id>/v<version>.yaml` under `root`, validating on the way out.

    Raises `FileNotFoundError` if the version does not exist. Runs `validate()` (with no
    `DeploymentAllowlist` -- this store has no way to obtain one; every load therefore
    carries Task 2's `ALLOWLIST_NOT_CHECKED` note) and raises `ValueError` if any finding
    is `error`-level, so a caller can never receive an `Artifact` back that the validator
    considers broken.

    Always returns `(artifact, findings)` (ruling E17) -- there is no opt-in call shape
    that hands back a bare `Artifact` looking fully checked when it is not. Every finding,
    at every level, is also logged before this function returns or raises, so a caller
    that only wants the artifact and never inspects the second element still gets the full
    picture in the log.
    """
    path = _artifact_path(root, id, version)
    if not path.exists():
        raise FileNotFoundError(f"no artifact at {path}")
    artifact = _parse_artifact(path)

    findings = validate(artifact)
    _log_findings(findings, artifact_id=id, version=version)
    errors = [f for f in findings if f.level == "error"]
    if errors:
        summary = "; ".join(f"{f.code}: {f.message}" for f in errors)
        raise ValueError(
            f"{id} v{version} failed load-time validation with {len(errors)} error(s): "
            f"{summary}"
        )

    return artifact, findings


def read_registry(root: Path) -> dict[str, dict[str, RegistryEntry]]:
    """Reads `artifacts/registry.json`, keyed by id then by version (as a string, since
    JSON object keys are always strings).

    Returns an empty dict if the registry does not exist yet -- a fresh store with no
    entries at all is not an error; there is simply nothing registered.
    """
    path = _registry_path(root)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {
        artifact_id: {
            version: RegistryEntry.model_validate(entry) for version, entry in versions.items()
        }
        for artifact_id, versions in raw.items()
    }


def _has_unverified_expect(artifact: Artifact) -> bool:
    """Acceptance criterion 6 / §8.3 step 5: any `Expect` this artifact carries that is
    not `verified` -- regardless of `source` -- disqualifies it from `approved`.

    Not narrowed to `source == "proposed"`: a `proposed` expect is always unverified (the
    model validator on `Expect` forbids the opposite combination), so checking `verified`
    directly already covers it, and also covers an `observed`/`authored` clause that
    simply has not been verified yet -- which criterion 6's wording ("any unverified
    expect") does not exempt.

    Walks every step and every expect on it (not `steps[0]` or `expects[0]` alone, per
    ruling I6): an unverified expect on a later step, or a later expect on a step that
    also carries an earlier, verified one, blocks approval exactly the same way.
    """
    return any(expect.verified is False for step in artifact.steps for expect in step.expects)


def _has_irreversible_step(artifact: Artifact) -> bool:
    """§6.4 / ruling E15: does any step of `artifact` carry `risk="irreversible"`?

    Walks every step, not just `steps[0]` -- an irreversible action anywhere in the
    capability makes `requires_human_approval=False` un-clearable for it.
    """
    return any(step.risk == "irreversible" for step in artifact.steps)


def write_registry_entry(
    root: Path, id: str, version: int, entry: RegistryEntry, *, artifact: Artifact | None = None
) -> None:
    """Writes `entry` into `artifacts/registry.json` at `(id, version)`, creating or
    replacing that one entry -- every other `(id, version)` already registered is left
    untouched.

    `artifact` is optional -- an operator may flip a registry entry's status without this
    call having the compiled artifact in hand (e.g. from the console, by id and version
    alone). It is an **optimisation** for a caller that already holds the artifact it just
    compiled, not a way to skip the gates below (ruling E18); passing an artifact that does
    not itself claim this `(id, version)` is refused rather than silently trusted.

    Resolution order (ruling E18), before any gate runs:

    1. If `artifact` is given, its `.id`/`.version` MUST equal the `id`/`version`
       arguments, or this raises `ValueError` -- closes I1's identity hole (an artifact
       for a different capability being used to clear this one's gates).
    2. Otherwise, if `artifacts/<id>/v<version>.yaml` exists, it is **parsed**
       (`_parse_artifact`, structural only -- deliberately not `load`, ruling E19): a
       `draft` registration must not depend on the file passing `validate()`.
    3. Otherwise, nothing is resolved -- there is no artifact anywhere to check.

    With something resolved, regardless of which of the two resolution paths produced it:

    - `status="approved"` is refused if the artifact holds any unverified `Expect`
      (acceptance criterion 6, §8.3 step 5), or if it fails load-time validation --
      any `error`-level finding from `validate()` (ruling E19; `_load_time_errors` reuses
      `validate()` itself rather than re-deriving what counts as an error). Neither check
      runs for `status="draft"`: an artifact still being iterated on, or one nobody has
      finished classifying yet, must still be registrable as a draft.
    - `requires_human_approval=False` is refused if the artifact holds any
      `risk="irreversible"` step (§6.4, ruling E15).

    With nothing resolved:

    - `status="approved"` is refused outright -- the store cannot approve a capability it
      does not hold any record of (closes I1's "ghost id" hole).
    - `requires_human_approval=False` is also refused (ruling E15's fallback clause): with
      no artifact to check, the store cannot prove no step is irreversible, and silently
      keeping the entry at `True` instead of raising would hide the caller's explicit
      disagreement with the store rather than surface it.
    - `status="draft"` with the field left at its default still writes; a fresh discovery
      run must be registrable before it has been compiled to disk or handed back to this
      call.
    """
    resolved: Artifact | None
    if artifact is not None:
        if artifact.id != id or artifact.version != version:
            raise ValueError(
                f"the supplied artifact is {artifact.id!r} v{artifact.version}, which "
                f"does not match the registry key ({id!r}, v{version}); "
                f"write_registry_entry only accepts the artifact actually being "
                f"registered under its own identity"
            )
        resolved = artifact
    elif _artifact_path(root, id, version).exists():
        resolved = _parse_artifact(_artifact_path(root, id, version))
    else:
        resolved = None

    if resolved is not None:
        if entry.status == "approved":
            if _has_unverified_expect(resolved):
                raise ValueError(
                    f"{id} v{version} cannot be marked approved: it holds at least one "
                    f"unverified expect (§8.3 step 5 -- the compiler cannot invent "
                    f"knowledge of a state it never observed)"
                )
            errors = _load_time_errors(resolved)
            if errors:
                summary = "; ".join(f"{f.code}: {f.message}" for f in errors)
                raise ValueError(
                    f"{id} v{version} cannot be marked approved: it fails load-time "
                    f"validation with {len(errors)} error(s): {summary}"
                )
        if entry.requires_human_approval is False and _has_irreversible_step(resolved):
            raise ValueError(
                f"{id} v{version} cannot clear requires_human_approval: it holds at "
                f"least one step classified risk='irreversible' (§6.4) -- an irreversible "
                f"capability may not run unattended, so this flag cannot be turned off "
                f"for it"
            )
    else:
        if entry.status == "approved":
            raise ValueError(
                f"{id} v{version} cannot be marked approved: no artifact is on disk for "
                f"this (id, version) and none was supplied to check against -- the store "
                f"cannot approve a capability it does not hold"
            )
        if entry.requires_human_approval is False:
            raise ValueError(
                f"{id} v{version} cannot clear requires_human_approval: no artifact is on "
                f"disk for this (id, version) and none was supplied to check against -- "
                f"the store cannot prove no step is irreversible, so the flag stays "
                f"un-clearable until it can be checked (§6.4, ruling E15)"
            )

    path = _registry_path(root)
    raw: dict[str, dict[str, Any]] = json.loads(path.read_text()) if path.exists() else {}
    raw.setdefault(id, {})[str(version)] = entry.model_dump(mode="json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, indent=2, sort_keys=True))
