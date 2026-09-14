"""Load-time validation of a capability contract (spec §4.4).

Runs before any replay. It is cheap, highly testable, and it catches the artifact defects
that would otherwise surface as mysterious replay failures -- a `from_input` naming an
input nobody declared does not fail at load today, it fails three steps into a replay as
an empty field on a form.

Two properties govern the whole module:

*`validate` returns findings; it never raises, and never stops at the first problem.* A
human reviewing an artifact wants the entire list, not whichever defect happened to be
checked first. A caller decides what to do with them -- `any(f.level == "error")` is the
gate; warnings and notes are for the reviewer.

*Different defects get different codes, even when a single spec sentence covers both.* A
`from_step` forward reference is fixed by reordering steps and a `from_step` cycle is
fixed by breaking the loop, so collapsing both into "invalid reference" would throw away
the only part of the finding a caller could act on.

*Acceptance criterion 1 -- no hostname, credential, CSS selector, XPath or regular
expression in the artifact -- is a finding family here, not a check the store owns.* Ruling
E22: `save`, `load` and the registry's approval gate all inherit it from this one place
(`CRITERION_1_CODES`), so a property of *the artifact* is enforced identically whichever
door the artifact comes through. Before E22 the pattern detectors lived in `store.py` and ran
on the write path only, so a file hand-edited on disk loaded clean.

Like `cua/artifact/models.py`, nothing here imports a browser driver, references a DOM, or
carries a CSS selector or an XPath. Only the first of those is mechanically enforced:
`tests/test_architecture.py` greps `cua/artifact/` for an `import`/`from` of `Playwright`
or `Selenium` and nothing else. The rest is convention held up by review, and saying so is
the point -- an overstated guarantee in a docstring is the same defect phase 2 hit on its
protected-value validator, where a reader trusted a check that did not cover what the words
claimed.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from cua.artifact.models import (
    Artifact,
    CapabilityPolicy,
    FailureKind,
    FromInput,
    FromStep,
    LiteralValue,
    Step,
)
from cua.surface.models import ActionKind, Locator, is_protected_name

FindingLevel = Literal["error", "warning", "note"]

# Acceptance criterion 1's finding family (ruling E22). `save` refuses on exactly these codes
# and on nothing else: E19 deliberately lets a draft carrying `RISK_UNCLASSIFIED` be saved and
# registered, so "any error" is the wrong gate for the write path. `load` and the approval
# gate refuse on every error-level finding, which includes these.
CRITERION_1_CODES: frozenset[str] = frozenset({
    "FORBIDDEN_CONTENT",
    "LITERAL_FROM_PROTECTED_FIELD",
    "LITERAL_WITHOUT_LOCATOR",
})


class Finding(BaseModel):
    """One thing the validator has to say about an artifact.

    `level` is the only field a caller must branch on: `error` means the artifact must not
    be replayed or approved, `warning` means a human should look (spec §4.4 puts `ordinal`
    use in this class deliberately -- it is the sanctioned last resort, not a defect), and
    `note` records something the validator could *not* check, so that "not checked" can
    never be read as "checked and clean".

    `code` is a stable, greppable identifier; `message` is the sentence for a human;
    `where` points at the part of the artifact at fault (e.g. `steps[1] (s2)`), and is
    `None` for a finding about the artifact as a whole.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    level: FindingLevel
    code: str
    message: str
    where: str | None = None


class DeploymentAllowlist(BaseModel):
    """What a deployment permits: spec §6.1's per-instance allowlist.

    Defined here as the minimal shape §4.4's narrowing check needs, and no more. Phase 5
    owns the real allowlist engine and must **extend this type** rather than introduce a
    second one -- two allowlist shapes drifting apart is an authorization bug, and this
    validator is not the place it should be discovered.

    An allowlist is configuration, never part of the artifact (§6.1): the same artifact
    gets different permissions in a different tenant. Each field is the permitted set;
    an empty list permits nothing. `denied_paths` is evaluated first and wins, because
    legacy applications mutate state on GET -- `/account/close?id=1` sits inside an allowed
    origin and an allowed path prefix, which allow-only rules cannot express.
    """

    model_config = ConfigDict(extra="forbid")

    allowed_origins: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    denied_paths: list[str] = Field(default_factory=list)
    allowed_actions: list[ActionKind] = Field(default_factory=list)


# The credential-name rule itself lives in `cua.surface.models.is_protected_name` (E6): one
# security-relevant rule, one implementation, because a drift between two copies is a
# credential leaking past one of the two checks that exist to stop it.

# Turns a control's accessible name into the identifier shape `inputs` keys use, so
# "Security Answer" can be recognised as the field a declared `security_answer` input feeds.
#
# E7: this normalises and then requires **exact equality**, so it errs toward silence, not
# toward noise. "Security Answer" fires; "Enter your Security Answer", "Security Answer
# (required)", "SecurityAnswer" and "Answer" all do not. That is deliberate and must stay.
# This is the *secondary* signal for §4.4's sixth condition -- the token rule below it and
# §8.3 step 4's compiler-side refusal are the primary controls -- and a fuzzy name-to-input
# matcher would fire on ordinary fields. A backstop that cries wolf trains a reviewer to
# skim past the finding that matters, which degrades the very control it was added to
# support.
_NON_IDENTIFIER_RE = re.compile(r"[^a-z0-9]+")


def _as_input_key(name: str) -> str:
    return _NON_IDENTIFIER_RE.sub("_", name.lower()).strip("_")


def _locator_chain(locator: Locator) -> tuple[list[Locator], bool]:
    """Every locator reachable from `locator` (itself, its `scope` ancestors, its
    fallbacks), and whether that graph closes a loop.

    Both checks that walk a locator want the whole chain rather than just the outermost
    node. An `ordinal` buried in a `scope` is exactly as reviewable a fact as one on the
    locator itself, and a fallback is the path taken when a human is least likely to be
    watching (R18's reasoning), so a protected name hiding in one must not be invisible.

    Cycle-guarded by object identity, and the guard now does two jobs (E9): it still stops
    the walk from ever recursing forever, and it also reports *which kind* of revisit it
    saw. Two identity sets, not one, because a revisit through a diamond and a revisit that
    closes a loop are different facts, and only the current DFS `path` -- the identities of
    `current`'s own ancestors on the branch being walked right now -- can tell them apart:

    - `path` carries the current branch's ancestors. Seeing `current` again while it is
      still its own ancestor (`id(current) in path`) is a back edge -- a genuine cycle --
      recorded in the returned flag, and the walk does not recurse into it again.
    - `seen` carries every locator visited on any branch so far. Seeing `current` again
      when it is *not* on the current path (`id(current) in seen` but not in `path`) is the
      same locator reached twice through two different branches -- a diamond, not a loop --
      and is silently deduplicated exactly as before, with no cycle reported. This is the
      distinction `test_a_locator_diamond_is_not_reported_as_a_cycle` and
      `test_a_scope_and_fallback_diamond_meeting_at_one_locator_is_not_a_cycle` pin: a check
      keyed on "have I seen this before" alone cannot make it, and would fire on every
      diamond, which is exactly the over-firing hazard `ORDINAL_USED` already hit once.

    `a.scope = b; b.scope = a` and `a.fallbacks = [b]; b.fallbacks = [a]` are both
    constructible through plain Pydantic attribute assignment -- no `model_construct`
    bypass needed -- and an unguarded walk would raise `RecursionError` out of a `validate`
    that documents that it never raises. A cyclic chain cannot arrive from a YAML load, but
    Task 3 mutates locators in memory while resolving an overlay, which is where this would
    first be hit.

    Phase 2 left the same cycle open in fallback *resolution* as a deferred minor, on the
    understanding that constructing one required a Pydantic bypass. That was wrong --
    `fallbacks` is an ordinary assignable field and `a.fallbacks = [b]; b.fallbacks = [a]`
    is enough -- so `cua.surface.locators.resolve_against` now carries the same guard (E8),
    pruning a back edge silently there. E9 is this module's diagnostic for the same defect:
    a cycle pruned silently at resolution time, with nothing anywhere saying it was ever
    there, is the failure shape this project has rejected everywhere else -- so `validate`
    now reports it as `LOCATOR_FALLBACK_CYCLE` before an artifact ever reaches replay.
    """
    seen: set[int] = set()
    chain: list[Locator] = []
    cyclic = False

    def walk(current: Locator, path: frozenset[int]) -> None:
        nonlocal cyclic
        if id(current) in path:
            cyclic = True
            return
        if id(current) in seen:
            return
        seen.add(id(current))
        chain.append(current)
        branch = path | {id(current)}
        if current.scope is not None:
            walk(current.scope, branch)
        for fallback in current.fallbacks:
            walk(fallback, branch)

    walk(locator, frozenset())
    return chain, cyclic


def _where(index: int, step: Step) -> str:
    return f"steps[{index}] ({step.id})"


def _step_findings(artifact: Artifact) -> list[Finding]:
    """Per-step checks: input wiring, `into` binding, literals, ordinals, risk, expect codes."""
    findings: list[Finding] = []
    for index, step in enumerate(artifact.steps):
        where = _where(index, step)

        if isinstance(step.value, FromInput) and step.value.from_input not in artifact.inputs:
            findings.append(Finding(
                level="error", code="UNKNOWN_INPUT", where=where,
                message=(
                    f"step {step.id} draws its value from input "
                    f"{step.value.from_input!r}, which the artifact does not declare"
                ),
            ))

        # §4.2 decision 9: `into` binds a declared output, or a local prefixed with `_`.
        # Locals carry values between steps and are never returned to the caller, so they
        # need no declaration -- the underscore is the declaration.
        binds_a_local = step.into is not None and step.into.startswith("_")
        if step.into is not None and not binds_a_local and step.into not in artifact.outputs:
            findings.append(Finding(
                level="error", code="UNKNOWN_OUTPUT", where=where,
                message=(
                    f"step {step.id} binds into {step.into!r}, which is neither a "
                    f"declared output nor a local (locals start with '_')"
                ),
            ))

        # Every step, not only those with a locator (E20): a literal on a locator-less step
        # is itself a finding, and the old guard here was what let it through unchecked.
        findings.extend(_literal_findings(artifact, step, where))

        if step.locator is not None:
            chain, cyclic = _locator_chain(step.locator)
            if cyclic:
                # E9: not one of §4.4's literal seven, kept for the same reason
                # DUPLICATE_STEP_ID is -- the machinery downstream (replay's fallback
                # resolution, E8's guard) cannot function correctly on a locator whose
                # scope/fallback graph closes a loop, and today it validates as clean and
                # is then pruned silently at resolution time with no diagnostic trail.
                findings.append(Finding(
                    level="error", code="LOCATOR_FALLBACK_CYCLE", where=where,
                    message=(
                        f"step {step.id}'s locator forms a cycle through its scope "
                        f"and/or fallback chain; it can never resolve and must be fixed "
                        f"before this artifact is replayed"
                    ),
                ))
            if any(link.ordinal is not None for link in chain):
                # §3.4 rule 2: ordinal is the sanctioned last resort when nothing else
                # disambiguates, so this is a warning a reviewer reads, not a defect.
                findings.append(Finding(
                    level="warning", code="ORDINAL_USED", where=where,
                    message=(
                        f"step {step.id} disambiguates by ordinal; it will break if the "
                        f"application reorders matching controls"
                    ),
                ))

        if step.risk is None:
            # Carried out of Task 1's review. §6.3 states the asymmetry deliberately:
            # over-classification costs a human a moment at approval time, while a genuine
            # transfer classified as safe and executed unattended is the error that cannot
            # be undone. `Step.risk` is nullable so an omission reads as "nobody has
            # classified this" rather than silently defaulting to `safe` -- and this is
            # what stops an unclassified step from later being *treated* as safe.
            findings.append(Finding(
                level="error", code="RISK_UNCLASSIFIED", where=where,
                message=(
                    f"step {step.id} has no risk classification; an unclassified step "
                    f"must not be replayable or approvable"
                ),
            ))

        # E4'/E17: a `fail` clause's `code` names the failure kind it declares, and a
        # `business` clause's `code` names the caller-facing business outcome. Neither
        # vocabulary is enforced at the model -- `Expect.code` is a plain `str | None` so
        # `business` and `fail` can share the one field -- so this is where an artifact
        # that names no failure kind, or an unknown one, is refused before replay.
        for expect in step.expects:
            if expect.outcome == "fail" and expect.code not in get_args(FailureKind):
                findings.append(Finding(
                    level="error", code="FAIL_CODE_NOT_A_FAILURE_KIND", where=where,
                    message=(
                        f"step {step.id} has a `fail` expect whose code "
                        f"{expect.code!r} is not one of the declared FailureKind values"
                    ),
                ))
            if expect.outcome == "business" and expect.code is None:
                findings.append(Finding(
                    level="error", code="BUSINESS_CODE_MISSING", where=where,
                    message=(
                        f"step {step.id} has a `business` expect with no code; a "
                        f"business outcome must name the code the caller receives"
                    ),
                ))
    return findings


def _literal_findings(artifact: Artifact, step: Step, where: str) -> list[Finding]:
    """§4.4's sixth condition: no literal originates from a protected or sensitive field.

    §4.2 decision 8 and §8.3 step 4 put the primary control in the compiler, which promotes
    a literal matching a declared input to `from_input` and refuses to persist a literal
    read from a protected or sensitive field. This is the load-time backstop for one that
    got through -- a hand-edited artifact, or an artifact compiled before that rule existed.

    Two signals are available once the artifact is on disk, and both are errors under the
    one code because both describe the same defect:

    1. The step's locator names a protected control -- `is_protected_name`, the single
       shared implementation of the rule phase 2's parser also uses (E6). This is the
       primary of the two.
    2. The control's name, normalised, is **exactly** a declared input's key and that input
       is `sensitive: true`. Secondary, and deliberately narrow: see `_as_input_key` (E7)
       for why it errs toward silence.

    What is *not* available: the live node's `protected` state, which existed only in the
    observation the compiler saw. That, plus both signals' documented blind spots (see
    `is_protected_name`), is precisely why the compiler holds the primary control and this
    is only the backstop.

    A literal on a step with **no locator** is `LITERAL_WITHOUT_LOCATOR`, an error (ruling
    E20, closing C1). Both signals above need a control name to look at, so with no locator
    there is nothing to check the literal against -- and an earlier revision returned `[]`
    here, the same value it returns for "checked, found nothing", so a credential literal on
    a locator-less `press_key` step validated clean and was written to disk verbatim. The
    empty list must never mean "could not check". Fail-closed costs nothing real: every
    value-consuming replay action (`fill`, `select`, `press_key`) acts on a resolved handle,
    so a locator-less literal is unreplayable as well as uncheckable. Cost if wrong: a
    future page-level keystroke action needs a deliberate change here, not a silent one.
    """
    if not isinstance(step.value, LiteralValue):
        return []
    if step.locator is None:
        return [Finding(
            level="error", code="LITERAL_WITHOUT_LOCATOR", where=where,
            message=(
                f"step {step.id} carries a literal value but no locator; the literal "
                f"cannot be checked against the control it fills, and no replay action "
                f"can consume it without one"
            ),
        )]

    chain, _ = _locator_chain(step.locator)
    for link in chain:
        if is_protected_name(link.name):
            return [Finding(
                level="error", code="LITERAL_FROM_PROTECTED_FIELD", where=where,
                message=(
                    f"step {step.id} fills a literal into a control named {link.name!r}, "
                    f"which names a credential field; use an input instead"
                ),
            )]

    if step.locator.name:
        key = _as_input_key(step.locator.name)
        declared = artifact.inputs.get(key)
        if declared is not None and declared.sensitive:
            return [Finding(
                level="error", code="LITERAL_FROM_PROTECTED_FIELD", where=where,
                message=(
                    f"step {step.id} fills a literal into a control matching input "
                    f"{key!r}, which is declared sensitive; use that input instead"
                ),
            )]
    return []


# --- Acceptance criterion 1: the pattern detectors (rulings E16 and E22) ------------------
#
# These ran in `store.py` on the write path alone until E22 moved them here, so that `save`,
# `load` and the approval gate enforce one rule from one place. They are pattern-based and
# their blind spots are documented on each, in the style `is_protected_name` uses, because a
# detector whose limits are unstated is one a reader will over-trust. The credential leg is
# not here: it is `_literal_findings` above, the same §4.4 sixth-condition check.

# A scheme prefix: "http://", "https://", "ws://", "ftp://", etc. Requires "://" so a
# locator's `role_name` strategy or an ordinary sentence ending in a colon never matches.
_URL_SCHEME_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]{1,15}://")

# An IPv4 address, with an optional ":port". Four dot-separated 1-3 digit groups is
# structural enough that it will not fire on a version string ("2.5") or a decimal id.
# Blind spot: IPv6 literals are not modelled.
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


def _iter_string_leaves(value: Any, path: str) -> Iterator[tuple[str, str]]:
    """Every `(path, text)` string leaf reachable from `value`, a `model_dump(mode="json")`
    tree. `path` is dotted-and-indexed (`steps[0].locator.rationale`) so a finding can point
    at the field at fault.

    Walks dict values and list items only. Dict *keys* are not yielded, and that is safe
    only because the schema constrains them: the artifact's two string-keyed maps,
    `inputs` and `outputs`, are typed `dict[IdentifierKey, ...]` in `models.py` (ruling E21)
    and a key that is not `^[a-z][a-z0-9_]*$` fails to parse. A hostname, URL, path or
    selector cannot be an identifier, so the key surface closes at parse time rather than
    by scanning. Before E21 this docstring's claim was false -- C2 saved an artifact whose
    input key was a URL -- which is why the constraint is at the model, not here.
    """
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_string_leaves(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_string_leaves(item, f"{path}[{index}]")


def _pattern_violations(text: str) -> list[str]:
    """What, if anything, in `text` acceptance criterion 1 forbids -- one entry per kind."""
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


def _forbidden_content_findings(artifact: Artifact) -> list[Finding]:
    """Acceptance criterion 1 over every string leaf of the artifact's serialized tree.

    Runs on `model_dump(mode="json")` -- the exact tree `save` writes -- so a hostname or
    selector arriving through *any* field is covered, including one a later phase adds
    without anyone remembering to extend a test fixture (E16's reasoning, now E22's home).

    **One code, `FORBIDDEN_CONTENT`, with the kind in `message`** rather than one code per
    kind. This module's rule is that different defects get different codes *when the fixes
    differ*; here they do not. A hostname in `description`, a CSS selector in `rationale`
    and an XPath in `name` are all fixed the same way -- delete the value, or replace it
    with the path or accessible name the artifact is allowed to carry -- so per-kind codes
    would give a caller nothing it could act on differently, while making `CRITERION_1_CODES`
    a set that must be kept in lockstep with the detector list, which is exactly the kind of
    drift a single code cannot suffer. The kind is still in every message, and `where`
    names the field, so a reviewer loses nothing.

    **One declared exemption, and only one.** `inputs.<name>.pattern` is the one regular
    expression an artifact may carry (`InputSpec.pattern`); it is scanned like any other
    leaf and passes because a JSON Schema validation pattern is not shaped like a host or
    a selector. There used to be a second exemption here, for `policy.allowed_origins[*]`,
    but E25 removed `CapabilityPolicy.allowed_origins` outright: narrowing on origin needs
    a hostname to compare against, and §6.1 and §4.2 decision 4 both rule out storing one
    in the artifact. With the field gone, a `policy` block narrows only paths and actions,
    and neither shape needs an exemption from this scan.

    A cyclic locator graph cannot be serialized (`model_dump` raises), so on that artifact
    the scan does not run and says so with a `note` (E4: "not checked" is never silent).
    The cycle itself is already `LOCATOR_FALLBACK_CYCLE`, an error, so nothing gating on
    errors can be misled by the note.
    """
    try:
        data: dict[str, Any] = artifact.model_dump(mode="json")
    except ValueError:
        return [Finding(
            level="note", code="FORBIDDEN_CONTENT_NOT_CHECKED", where=None,
            message=(
                "the artifact could not be serialized (its locator graph closes a loop), "
                "so it was NOT scanned for forbidden content; do not read this result as "
                "scanned and clean"
            ),
        )]
    findings: list[Finding] = []
    for path, text in _iter_string_leaves(data, ""):
        for hit in _pattern_violations(text):
            findings.append(Finding(
                level="error", code="FORBIDDEN_CONTENT", where=path,
                message=(
                    f"{path} carries {hit}; an artifact never holds a hostname, an "
                    f"address, a URL, a CSS selector or an XPath (acceptance criterion 1)"
                ),
            ))
    return findings


def _output_findings(artifact: Artifact) -> list[Finding]:
    """§4.4: every declared output has a producing step."""
    produced = {step.into for step in artifact.steps if step.into is not None}
    return [
        Finding(
            level="error", code="OUTPUT_NEVER_PRODUCED", where=f"outputs.{name}",
            message=(
                f"output {name!r} is declared but no step binds into it; a caller would "
                f"always receive it empty"
            ),
        )
        for name in artifact.outputs
        if name not in produced
    ]


def _from_step_findings(artifact: Artifact) -> list[Finding]:
    """§4.4: `from_step` references form a directed acyclic graph.

    Reported as three distinct codes because the three defects have three different fixes:
    `FROM_STEP_UNKNOWN_STEP` (the id names no step at all -- a typo or a deleted step),
    `FROM_STEP_FORWARD_REFERENCE` (the value is consumed before the step producing it runs
    -- reorder), and `FROM_STEP_CYCLE` (the chain closes on itself -- break the loop).

    A step carries at most one `from_step`, so the reference graph is functional: following
    the single outgoing edge from each step either terminates or revisits a step already on
    the walk, and no general cycle-finding machinery is warranted.

    A step referencing *itself* is a cycle and is deliberately **not** also a forward
    reference -- hence `>` and not `>=` on the position test below. No reordering fixes a
    self-reference, so calling it a forward reference would hand the reviewer the one fix
    that cannot work.
    """
    findings: list[Finding] = []

    position: dict[str, int] = {}
    for index, step in enumerate(artifact.steps):
        if step.id in position:
            # Not one of §4.4's seven conditions, but the graph below cannot be built
            # without resolving an id to a step, and Task 3's overlays key on step id too.
            # Silently keeping one of two steps sharing an id is the wrong answer to an
            # ambiguity a human can fix in a second.
            findings.append(Finding(
                level="error", code="DUPLICATE_STEP_ID", where=_where(index, step),
                message=(
                    f"step id {step.id!r} is used more than once; `from_step` and overlay "
                    f"keys address a step by id, so it must be unique"
                ),
            ))
            continue
        position[step.id] = index

    edges: dict[str, str] = {}
    for index, step in enumerate(artifact.steps):
        if not isinstance(step.value, FromStep):
            continue
        where = _where(index, step)
        target = step.value.from_step
        if target not in position:
            findings.append(Finding(
                level="error", code="FROM_STEP_UNKNOWN_STEP", where=where,
                message=(
                    f"step {step.id} draws its value from step {target!r}, which does "
                    f"not exist"
                ),
            ))
            continue
        if position[target] > index:
            findings.append(Finding(
                level="error", code="FROM_STEP_FORWARD_REFERENCE", where=where,
                message=(
                    f"step {step.id} draws its value from step {target!r}, which runs "
                    f"later; steps execute in list order"
                ),
            ))
        edges.setdefault(step.id, target)

    reported: set[frozenset[str]] = set()
    for start in position:
        walked: list[str] = []
        node: str | None = start
        while node is not None and node not in walked:
            walked.append(node)
            node = edges.get(node)
        if node is None:
            continue
        members = frozenset(walked[walked.index(node):])
        if members in reported:
            continue
        reported.add(members)
        findings.append(Finding(
            level="error", code="FROM_STEP_CYCLE", where=None,
            message=(
                "from_step references form a cycle through "
                + ", ".join(sorted(members))
                + "; no step in it can ever produce a value"
            ),
        ))
    return findings


def _permits_path(path: str, prefixes: list[str]) -> bool:
    return any(path.startswith(prefix) for prefix in prefixes)


def _narrowing_findings(
    policy: CapabilityPolicy | None, deployment: DeploymentAllowlist
) -> list[Finding]:
    """§4.4 and §6.1: a per-capability policy narrows the deployment allowlist, never widens it.

    A `None` field on the policy means "not narrowed", not "narrowed to nothing" (see
    `CapabilityPolicy`), so it inherits the deployment's own set and cannot widen anything.
    A stated field widens when it names something the deployment does not permit.

    Paths are compared by prefix, which is what §6.1's "allowed and denied path patterns"
    amounts to for the literal prefixes this validator can see; deny is checked first and
    wins, because a capability policy re-permitting a denied path is exactly the widening
    that matters (`/teller/admin/close` sits inside an allowed origin and an allowed
    prefix). Phase 5 owns richer pattern matching and inherits this type.

    **The deny check is deliberately one-directional, and the asymmetry is not an
    oversight.** A policy path *inside* a denied prefix (`/teller/admin/close` against
    denied `/teller/admin/`) is reported, because the policy is trying to permit a
    specifically denied thing. A policy path that *encloses* a denied subtree (`/teller/`
    against denied `/teller/admin/`) is not, because it is not a widening: deny wins at
    enforcement regardless, so `/teller/` grants the capability `/teller/` minus
    `/teller/admin/`, exactly as it would have without the policy. Reporting it would mean
    erroring on the common, correct case of a policy naming a broad prefix that happens to
    contain a carve-out. Pinned in both directions by
    `test_a_policy_path_inside_a_denied_prefix_widens_and_is_an_error` and
    `test_a_policy_path_enclosing_a_denied_subtree_is_not_reported`.
    """
    if policy is None:
        return []

    findings: list[Finding] = []
    for path in policy.allowed_paths or []:
        if _permits_path(path, deployment.denied_paths):
            findings.append(Finding(
                level="error", code="POLICY_WIDENS_ALLOWLIST", where="policy.allowed_paths",
                message=(
                    f"the capability policy permits path {path!r}, which this deployment "
                    f"denies; deny rules are evaluated first and win"
                ),
            ))
        elif not _permits_path(path, deployment.allowed_paths):
            findings.append(Finding(
                level="error", code="POLICY_WIDENS_ALLOWLIST", where="policy.allowed_paths",
                message=(
                    f"the capability policy permits path {path!r}, which this deployment "
                    f"does not; a policy may only narrow"
                ),
            ))
    for action in policy.allowed_actions or []:
        if action not in deployment.allowed_actions:
            findings.append(Finding(
                level="error", code="POLICY_WIDENS_ALLOWLIST", where="policy.allowed_actions",
                message=(
                    f"the capability policy permits action {action!r}, which this "
                    f"deployment does not; a policy may only narrow"
                ),
            ))
    return findings


def _not_checked_finding(policy: CapabilityPolicy | None) -> Finding:
    """E4: say out loud that the narrowing check did not run.

    The deployment allowlist engine is phase 5's, so `validate` can be called before one
    exists. A caller must never be able to mistake "not checked" for "checked and clean",
    which is the whole reason this is a finding rather than a silent skip.

    `Artifact.policy` is nullable precisely so the two cases stay distinguishable after
    parsing, and the message keeps that distinction: with no policy block there is nothing
    to narrow and nothing at risk, while a declared policy carries an unverified claim.
    """
    if policy is None:
        return Finding(
            level="note", code="ALLOWLIST_NOT_CHECKED", where=None,
            message=(
                "no deployment allowlist was supplied, so policy narrowing was not "
                "checked; this artifact declares no policy block, so there is nothing to "
                "narrow"
            ),
        )
    return Finding(
        level="note", code="ALLOWLIST_NOT_CHECKED", where="policy",
        message=(
            "no deployment allowlist was supplied, so this artifact's declared policy was "
            "NOT checked for widening; do not read this result as checked and clean"
        ),
    )


def validate(artifact: Artifact, deployment: DeploymentAllowlist | None = None) -> list[Finding]:
    """Runs every §4.4 check over `artifact` and returns what it found, worst first.

    Never raises and never short-circuits: a caller gets every defect in one pass. Gate on
    `any(f.level == "error" for f in validate(a))`; do not gate on the list being empty,
    because a clean artifact still yields the `ORDINAL_USED` warnings and the E4 note.

    `deployment` is optional because the allowlist engine is phase 5's. Omitting it skips
    only the narrowing check, and says so with a `note`-level finding.
    """
    findings = _step_findings(artifact)
    findings.extend(_forbidden_content_findings(artifact))
    findings.extend(_output_findings(artifact))
    findings.extend(_from_step_findings(artifact))
    if deployment is None:
        findings.append(_not_checked_finding(artifact.policy))
    else:
        findings.extend(_narrowing_findings(artifact.policy, deployment))

    order: dict[FindingLevel, int] = {"error": 0, "warning": 1, "note": 2}
    return sorted(findings, key=lambda f: order[f.level])
