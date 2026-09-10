"""Pure data vocabulary for describing controls on an application screen.

No browser concepts appear anywhere in this module: not Playwright, not CSS, not XPath.
`tests/test_architecture.py` enforces that only `cua/surface/web.py` may import a browser
driver, and every other phase imports this module, so it has to stay importable with no
browser library installed at all.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

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
    role: str
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


class NodeState(BaseModel):
    """Boolean flags observed for a control at snapshot time."""

    disabled: bool = False
    checked: bool = False
    expanded: bool = False
    protected: bool = False


class Node(BaseModel):
    """One control as it appeared in a single accessibility-tree snapshot."""

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
        # screen. This lives on the model, not in the parser, because a model-level
        # validator runs on every construction path, present and future; a parser-side
        # check is one new call site away from being silently skipped.
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
