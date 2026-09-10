"""Pure data vocabulary for describing controls on an application screen.

No browser concepts appear anywhere in this module: not Playwright, not CSS, not XPath.
`tests/test_architecture.py` enforces that only `cua/surface/web.py` may import a browser
driver, and every other phase imports this module, so it has to stay importable with no
browser library installed at all.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Confidence = Literal["high", "medium", "low"]
NameMatch = Literal["exact", "contains", "prefix"]
Strategy = Literal["role_name", "text", "ax_path"]


class SurfaceSegment(BaseModel):
    """One hop in a control's path from the top-level window into nested frames."""

    kind: Literal["window", "frame"]
    name: str


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
