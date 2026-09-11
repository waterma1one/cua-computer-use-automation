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

Like `cua/artifact/models.py`, nothing here imports a browser driver, references a DOM, or
carries a CSS selector or an XPath. Only the first of those is mechanically enforced:
`tests/test_architecture.py` greps `cua/artifact/` for an `import`/`from` of `playwright`
or `selenium` and nothing else. The rest is convention held up by review, and saying so is
the point -- an overstated guarantee in a docstring is the same defect phase 2 hit on its
protected-value validator, where a reader trusted a check that did not cover what the words
claimed.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cua.artifact.models import Artifact, CapabilityPolicy, FromInput, FromStep, LiteralValue, Step
from cua.surface.models import ActionKind, Locator, is_protected_name

FindingLevel = Literal["error", "warning", "note"]


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


def _locator_chain(locator: Locator) -> list[Locator]:
    """Every locator reachable from `locator`: itself, its `scope` ancestors, its fallbacks.

    Both checks that walk a locator want the whole chain rather than just the outermost
    node. An `ordinal` buried in a `scope` is exactly as reviewable a fact as one on the
    locator itself, and a fallback is the path taken when a human is least likely to be
    watching (R18's reasoning), so a protected name hiding in one must not be invisible.

    Cycle-guarded by object identity. `a.scope = b; b.scope = a` is constructible through
    plain Pydantic attribute assignment -- no `model_construct` bypass needed -- and an
    unguarded walk would raise `RecursionError` out of a `validate` that documents that it
    never raises. A cyclic chain cannot arrive from a YAML load, but Task 3 mutates locators
    in memory while resolving an overlay, which is where this would first be hit.

    Phase 2 left the same cycle open in fallback *resolution* as a deferred minor, on the
    understanding that constructing one required a Pydantic bypass. That was wrong --
    `fallbacks` is an ordinary assignable field and `a.fallbacks = [b]; b.fallbacks = [a]`
    is enough -- so `cua.surface.locators.resolve_against` now carries the same guard (E8).
    """
    seen: set[int] = set()
    chain: list[Locator] = []

    def walk(current: Locator) -> None:
        if id(current) in seen:
            return
        seen.add(id(current))
        chain.append(current)
        if current.scope is not None:
            walk(current.scope)
        for fallback in current.fallbacks:
            walk(fallback)

    walk(locator)
    return chain


def _where(index: int, step: Step) -> str:
    return f"steps[{index}] ({step.id})"


def _step_findings(artifact: Artifact) -> list[Finding]:
    """Per-step checks: input wiring, `into` binding, literals, ordinals, risk."""
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

        if step.locator is not None:
            findings.extend(_literal_findings(artifact, step, where))
            if any(link.ordinal is not None for link in _locator_chain(step.locator)):
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
    """
    if not isinstance(step.value, LiteralValue) or step.locator is None:
        return []

    for link in _locator_chain(step.locator):
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
    for origin in policy.allowed_origins or []:
        if origin not in deployment.allowed_origins:
            findings.append(Finding(
                level="error", code="POLICY_WIDENS_ALLOWLIST", where="policy.allowed_origins",
                message=(
                    f"the capability policy permits origin {origin!r}, which this "
                    f"deployment does not; a policy may only narrow"
                ),
            ))
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
    findings.extend(_output_findings(artifact))
    findings.extend(_from_step_findings(artifact))
    if deployment is None:
        findings.append(_not_checked_finding(artifact.policy))
    else:
        findings.extend(_narrowing_findings(artifact.policy, deployment))

    order: dict[FindingLevel, int] = {"error": 0, "warning": 1, "note": 2}
    return sorted(findings, key=lambda f: order[f.level])
