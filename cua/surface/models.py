"""Pure data vocabulary for describing controls on an application screen.

No browser concepts appear anywhere in this module: not Playwright, not CSS, not XPath.
`tests/test_architecture.py` enforces that only `cua/surface/web.py` may import a browser
driver, and every other phase imports this module, so it has to stay importable with no
browser library installed at all.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Confidence = Literal["high", "medium", "low"]
# "any" matches whatever the accessible name is (including none); it is only meaningful on a
# `Locator` with a role, and is how a control is identified by role and position alone when
# its own text is a per-input value.
NameMatch = Literal["exact", "contains", "prefix", "any"]
Strategy = Literal["role_name", "text", "ax_path"]

# Credential-token vocabulary used to infer that a control holds a secret from its
# accessible name alone. Deliberately extensible -- add tokens here as new leaky labels turn
# up in real applications.
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

# Private to this module by E6. The rule this compiles is the rule, and it must have exactly
# one implementation -- see `is_protected_name`.
_PROTECTED_NAME_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(token) for token in PROTECTED_NAME_TOKENS) + r")\b",
    re.IGNORECASE,
)


def is_protected_name(name: str | None) -> bool:
    """Does this accessible name indicate a control holding a credential?

    E6, following R22's precedent: one security-relevant rule, one implementation. Two
    layers need this predicate -- `cua.surface.snapshot` infers `Node.state.protected` from
    it while parsing aria-snapshot YAML, and `cua.artifact.validate` uses it for spec §4.4's
    sixth condition (no literal originates from a protected field). Both had their own
    byte-identical compiled copy, and nothing held the two equal; a drift between them is a
    credential leaking past one of the two checks that exist to stop it. It lives here, in
    `models.py`, beside the vocabulary it matches, because `models.py` is already a
    dependency both layers take and the artifact layer has no business importing an
    accessibility-YAML parser to reach it.

    Matched case-insensitively **on word boundaries, never as a substring**: a naive
    substring match on "pin" would make "Shipping" and "Spinner" credential fields.

    Three blind spots, all of them consequences of inferring protection from a name alone.
    Every example below was executed against this predicate, and each of the three has a
    test in `tests/surface/test_models.py`, so the next reader meets them on purpose rather
    than assuming coverage:

    1. A credential field labelled with none of these tokens: "Passphrase", "Memorable
       Word", "Security Answer" and "Card Verification Code" are all real credential labels
       that return `False`. (An earlier draft of this docstring offered "Secret Word" as the
       example. It returns `True` -- `secret` is in the tuple and sits on word boundaries --
       so the example of a miss was a string the rule catches. Measured, not assumed, is the
       standard here.)
    2. A credential field with **no accessible name at all** (`name is None`), where there
       is nothing for this to look at.
    3. Plurals and compounds: "Passwords", "PINs", "password_field" and "MyPassword" all
       fail the word-boundary test. This matters more on the artifact side than the parser
       side -- a persisted locator name is likelier to carry a plural than a live field's
       label is -- and widening the rule to catch them is what would reintroduce "Shipping".

    Because of all three, this is a backstop and never the only control: a live node's
    `protected` state comes from the parser, and §8.3 step 4 puts the primary refusal in the
    compiler, which can still see the observation the artifact was built from.
    """
    if not name:
        return False
    return _PROTECTED_NAME_RE.search(name) is not None


class SurfaceSegment(BaseModel):
    """One hop in a control's path from the top-level window into nested frames.

    R26: `kind` includes `"pane"` alongside the web surface's own `"window"`/`"frame"` so
    that a desktop surface (spec §3.3: "the same shape generalizes to a desktop surface,
    where the path is window then pane") can be expressed without editing this module. Only
    `cua/surface/web.py` ever constructs `"window"`/`"frame"` segments today; `"pane"` is
    reserved for a future desktop `Surface` implementation.
    """

    kind: Literal["window", "frame", "pane"]
    name: str


def ancestor_positions(position: int, depths: Sequence[int]) -> list[int]:
    """Returns the indices into `depths` that enclose `depths[position]`, nearest first.

    R22: the one containment walk shared by `cua.surface.snapshot._scrub_ancestor_names`
    (walking parsed-entry depths to scrub a protected value out of its ancestors' names,
    R12) and `cua.surface.locators.ancestors_of` (walking `Node` depths to compute
    containment for locator synthesis and resolution, R16). Both used to carry their own
    copy of this backward, running-minimum-depth walk; a drift between the two copies is a
    credential leak on one side and a wrong scope on the other, so there is now exactly one
    implementation. It lives here, beside `Node`, rather than in a new module, because
    `models.py` is already a dependency both `snapshot.py` and `locators.py` take, and R6
    keeps `locators.py` free of importing `snapshot.py`.

    Walking backward from `position` with a running minimum depth (starting at
    `depths[position]`), every earlier index whose depth is strictly below the running
    minimum is an ancestor, and the minimum drops to its depth; an earlier index at the same
    or greater depth is a sibling (or a descendant of one) and is skipped without breaking
    the walk, which is what lets it reach a grandparent past an intervening sibling.

    Callers must pass `depths` in true document (pre-order) order, where every node's
    ancestors precede it and depth only rises and falls with real nesting -- see
    `ancestors_of`'s docstring for what a caller gets back if that invariant does not hold.
    """
    running_min = depths[position]
    ancestors: list[int] = []
    for i in range(position - 1, -1, -1):
        if depths[i] < running_min:
            ancestors.append(i)
            running_min = depths[i]
    return ancestors


class Require(BaseModel):
    """Preconditions a locator's target must satisfy before it is acted on."""

    visible: bool = True
    enabled: bool = True


class Locator(BaseModel):
    """A recipe for re-finding a control, carrying its own justification.

    Strategies are accessibility-tree concepts only (role+name, visible text, or an
    accessibility-tree path) -- never CSS or XPath. The target application is legacy
    markup with no stable selectors, so a locator vocabulary that could express a CSS
    selector would eventually grow one; `test_locator_serializes_without_any_browser_
    concept` pins that this stays a browser-free description.
    """

    strategy: Strategy = "role_name"
    # role is None-able because a text-strategy locator exists precisely to match a node
    # that has no accessible name worth relying on, in a target application that carries
    # no ARIA roles at all -- forcing a role there would make the synthesizer fabricate
    # one, and that fabricated value ends up in an artifact a human is meant to review.
    # Only role_name locators are meaningless without a role, so only that case is
    # enforced below.
    role: str | None = None
    name: str | None = None
    name_match: NameMatch = "exact"
    surface_path: list[SurfaceSegment]
    scope: Locator | None = None
    ordinal: int | None = None
    require: Require = Field(default_factory=Require)
    fallbacks: list[Locator] = Field(default_factory=list)
    # rationale and confidence are required, not defaulted: a locator without a stated
    # reason for how it identifies its control cannot be reviewed.
    rationale: str
    confidence: Confidence

    @model_validator(mode="after")
    def _role_name_strategy_requires_a_role(self) -> Locator:
        if self.strategy == "role_name" and self.role is None:
            raise ValueError("a role_name locator requires a role")
        return self

    @model_validator(mode="after")
    def _any_name_match_requires_a_role_and_no_name(self) -> Locator:
        if self.name_match == "any" and (self.role is None or self.name is not None):
            raise ValueError('name_match "any" requires a role and no name')
        return self

    @model_validator(mode="after")
    def _ordinal_is_not_negative(self) -> Locator:
        # Also fix: a negative ordinal is exactly the DOM-ish positional index spec §3.4
        # rule 1 outlaws, and the two negative values did not even agree with each other --
        # -2 resolved (Python's negative-slice semantics picked the second-to-last match),
        # while -1 always came back not_found (`matches[-1:0]` is empty). Constrained here,
        # at construction, rather than left as an implicit resolution-time accident.
        if self.ordinal is not None and self.ordinal < 0:
            raise ValueError("ordinal must be >= 0")
        return self

    @model_validator(mode="after")
    def _fallbacks_share_this_locators_surface_path(self) -> Locator:
        # R18: a fallback must resolve against the same surface as the locator it backs.
        # R8's whole premise is that identity and uniqueness are scoped per surface_path;
        # letting a fallback cross that boundary would silently reopen the exact
        # same-name-different-frame ambiguity R8 exists to close, and it would do so on the
        # recovery path, where a human is least likely to be watching. Enforced here, at
        # construction, so a cross-frame fallback cannot exist at all -- not merely rejected
        # later at resolution time.
        for fallback in self.fallbacks:
            if fallback.surface_path != self.surface_path:
                raise ValueError(
                    "a fallback locator must carry the same surface_path as the locator "
                    "it backs"
                )
        return self


class NodeState(BaseModel):
    """Boolean flags observed for a control at snapshot time."""

    disabled: bool = False
    checked: bool = False
    expanded: bool = False
    protected: bool = False


class Node(BaseModel):
    """One control as it appeared in a single accessibility-tree snapshot."""

    # Revalidates on every attribute assignment (including replacing `state` wholesale),
    # not just at construction, so that e.g. `node.value = "..."` on a protected node is
    # caught the same way construction is. See the comment on the validator below for what
    # this does and does not cover.
    model_config = ConfigDict(validate_assignment=True)

    index: int
    role: str
    name: str | None = None
    value: str | None = None
    state: NodeState
    surface_path: list[SurfaceSegment]
    # Accessibility-tree nesting level: 0 for a top-level node, deeper for a node nested
    # inside a container (e.g. a row inside a table). Exists so a locator synthesizer can
    # later express "the control inside that row" without parent pointers -- the parser
    # that builds these from nested YAML fills it in from the nesting level it walked.
    depth: int = 0

    @model_validator(mode="after")
    def _protected_nodes_carry_no_value(self) -> Node:
        # Spec §3.7: accessibility snapshots emit a password field's live value verbatim
        # -- the browser does not redact it the way it redacts the rendered glyphs on
        # screen. This lives on the model, not in the parser, because a parser-side check
        # is one new call site away from being silently skipped.
        #
        # What this guarantee actually covers: normal construction (`Node(...)`),
        # `Node.model_validate(...)`, and -- because `validate_assignment=True` is set
        # above -- ordinary attribute assignment (`node.value = ...`, or replacing
        # `node.state` wholesale). It deliberately does NOT cover three bypasses:
        # `Node.model_construct(...)`, `node.model_copy(update={...})` -- both skip
        # validation entirely by Pydantic's design -- and mutating a field on the nested
        # `NodeState` in place (`node.state.protected = True`), which does not re-run this
        # validator because `validate_assignment` fires on `Node`'s own field assignments,
        # not on mutations of an object reached through one of them. A Node built, copied,
        # or mutated through any of the three can end up protected with a live value with
        # no error raised. Anything reaching for `model_copy(update=...)` on a Node -- a
        # redaction step or an evidence writer are the obvious candidates -- must not
        # assume this validator will catch a mistake there.
        if self.state.protected and self.value is not None:
            raise ValueError("a protected node must not carry a value")
        return self


class Observation(BaseModel):
    """Every control visible across all frames at one point in time."""

    generation: int
    nodes: list[Node]
    truncated: bool


class Unique(BaseModel):
    """A locator resolved to exactly one node."""

    kind: Literal["unique"] = "unique"
    node: Node


class NotFound(BaseModel):
    """A locator matched no node."""

    kind: Literal["not_found"] = "not_found"
    reason: str


class Ambiguous(BaseModel):
    """A locator matched more than one node."""

    kind: Literal["ambiguous"] = "ambiguous"
    count: int


class PreconditionFailed(BaseModel):
    """A locator resolved to one node, but it failed a `Require` check."""

    kind: Literal["precondition_failed"] = "precondition_failed"
    which: Literal["visible", "enabled"]


Resolution = Annotated[
    Unique | NotFound | Ambiguous | PreconditionFailed,
    Field(discriminator="kind"),
]

# The closed replay action vocabulary (spec §3.5). Closed, not open, because the policy
# engine in a later phase classifies risk per action kind -- an open set means an
# unclassifiable action, which means a hole in the allowlist.
ActionKind = Literal[
    "navigate",
    "click",
    "fill",
    "select",
    "press_key",
    "wait_for",
    "read",
    "dismiss_dialog",
]


class Action(BaseModel):
    """One step of a replayable action, thin by design (R5).

    `WebSurface` (`cua/surface/web.py`) is the only place this is executed against a real
    browser; phase 4's replay engine only needs to import this to build and inspect a
    trace, and it may not import Playwright to do so -- hence this lives here, not in
    `base.py`. Kept thin: phase 4 owns the replay engine and is expected to extend it.
    """

    kind: ActionKind
    locator: Locator | None = None
    value: str | None = None


class ActionResult(BaseModel):
    """The outcome of executing one `Action` against a surface."""

    ok: bool
    action: Action
    read_value: str | None = None


class EvidenceFrame(BaseModel):
    """A single point-in-time capture of a surface, for the evidence trail.

    `snapshot_yaml` is already scrubbed (spec §3.7.2's `scrub_protected_values`) by the
    time it reaches this model -- nothing downstream should have to scrub it again.
    """

    generation: int
    image_png: bytes | None = None
    snapshot_yaml: str
