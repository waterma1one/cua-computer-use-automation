"""The capability contract vocabulary: pure Pydantic models, no I/O.

Mirrors spec S4.1's on-disk artifact shape field for field. This module is pure data --
no cross-field validation beyond what the nine decisions in spec S4.2 and the ambiguity
resolutions in the phase-3 task brief actually require, and (like `cua/surface/models.py`)
nothing here imports a browser driver, references a DOM, or carries a CSS selector or an
XPath. Only the first of those is mechanically enforced: `tests/test_architecture.py` greps
the whole `cua/artifact/` package for an `import`/`from` of `Playwright` or `Selenium`, and
nothing else. The rest is convention held up by review. The distinction is stated rather
than glossed, because a docstring claiming more than its test delivers is how a reader
comes to trust a check that does not cover what the words say.
The single deliberate exception to "no regular expressions in an artifact" is
`InputSpec.pattern`, a JSON Schema validation pattern for a caller's argument, never a
locator.

Load-time cross-artifact validation (input/output wiring, DAG checks, policy narrowing)
is Task 2's job, not this module's. Tenant overlay resolution is Task 3's. Tool-schema
export is Task 4's. The registry (`status`/`stability`) is Task 5's -- which is exactly
why neither field exists on `Artifact` here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from cua.surface.models import ActionKind, Locator, NameMatch, Strategy, SurfaceSegment


class Matcher(BaseModel):
    """A predicate over what is currently on screen.

    Used by `Expect.when`, `Success.checkpoint`, and `Recovery.detect` -- never by a step
    that acts on a control. E1: a `when` clause is not a `Locator`. Forcing it into
    `Locator`'s shape would mean fabricating a `surface_path` and a `rationale` for
    something never acted on, in a file a human is meant to review, so `Matcher` carries
    only `strategy`, `role`, `name`, and `name_match` -- nothing else.

    Reuses phase 2's `Strategy` and `NameMatch` literals rather than redeclaring them, so
    the two vocabularies cannot drift apart; in particular, `NameMatch` has no `regex`
    member, so a matcher can never carry a regular expression either.
    """

    model_config = ConfigDict(extra="forbid")

    strategy: Strategy = "role_name"
    role: str | None = None
    name: str | None = None
    name_match: NameMatch = "exact"


class Target(BaseModel):
    """A `navigate` step's destination.

    E2, S4.2 decision 4: `path` only, never a host. An artifact carrying a host would be
    tenant-locked by construction; the base URL comes from per-instance configuration at
    replay time.
    """

    # E5: `extra="forbid"` across every model in this module (the three `StepValue` arms
    # already had it). This is a file a human authors and hand-edits, so a mistyped or
    # misplaced key -- `host` being the case this rule exists to catch -- must be a loud
    # parse failure, never a silently discarded field.
    model_config = ConfigDict(extra="forbid")

    path: str


class FromInput(BaseModel):
    """A step value drawn from a declared capability input, by name."""

    model_config = ConfigDict(extra="forbid")

    from_input: str


class LiteralValue(BaseModel):
    """A step value hardcoded at discovery time.

    S4.2 decision 8: a discovery run bakes a hardcoded value (e.g. one member ID) into a
    supposedly reusable capability, so the compiler promotes any literal matching a
    declared input to `from_input` and refuses to persist a literal read from a protected
    or sensitive field. That promotion is compiler logic for a later task; this model only
    has to be able to represent the literal itself.
    """

    model_config = ConfigDict(extra="forbid")

    literal: str


class FromStep(BaseModel):
    """A step value carried over from an earlier step's `into` binding."""

    model_config = ConfigDict(extra="forbid")

    from_step: str


# E3: presence discriminates, not a `kind:` tag field. A `kind:` discriminator would be
# easier to express in Pydantic, but it would change the artifact's on-disk shape, and
# that shape is the reviewable deliverable (spec S4.1 writes `{from_input: member_id}`,
# `{literal: "..."}`, `{from_step: s3}` with no tag). Each arm above sets
# `extra="forbid"` and declares exactly one required field, so a mapping carrying two of
# the three keys matches none of the arms and is rejected rather than silently resolved
# to whichever arm happened to match first.
StepValue = FromInput | LiteralValue | FromStep

# Ruling E21: the key of an `inputs`/`outputs` map is an identifier, `^[a-z][a-z0-9_]*$`,
# constrained here at the model rather than scanned for forbidden content downstream. §8.3
# step 4 makes parameter names model-authored metadata -- exactly the page-influenced text
# §6.7 worries about -- and a hostname, URL, path or selector cannot be an identifier, so the
# key surface closes at parse time. The tool-schema export gets clean property names for
# free. Cost if wrong: an upper-case or hyphenated name the model proposes must be normalised
# by phase 7's compiler, which is the direction §8.3 step 4 already implies (the model
# contributes names; the compiler decides).
IdentifierKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]

Risk = Literal["safe", "risky", "irreversible"]
Outcome = Literal["continue", "business", "retry", "fail"]
ExpectSource = Literal["observed", "proposed", "authored"]


class Expect(BaseModel):
    """One outcome clause attached to a step: "if the screen now matches `when`, the
    business outcome is `outcome`."
    """

    # `validate_assignment=True` follows phase 2's `Node` precedent (its protected-value
    # validator in `cua/surface/models.py`) so the proposed/verified check below reruns on
    # ordinary attribute assignment, not just construction -- see that validator's comment
    # for what this pattern does and does not cover, restated below for this model's own
    # fields. `extra="forbid"` is E5.
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    when: Matcher
    outcome: Outcome
    code: str | None = None
    # Required, not defaulted: a clause with no recorded provenance is exactly what this
    # field exists to prevent.
    source: ExpectSource
    # Defaults to unverified. S8.3 step 5: the compiler cannot invent knowledge of a state
    # it never itself observed, so the schema records the difference between a source that
    # saw the state and one that only proposed it, rather than hiding it behind a flag.
    verified: bool = False

    @model_validator(mode="after")
    def _a_proposed_expect_cannot_claim_to_be_verified(self) -> Expect:
        # What this guarantee actually covers: normal construction (`Expect(...)`),
        # `Expect.model_validate(...)`, and -- because `validate_assignment=True` is set
        # above -- ordinary attribute assignment to either field (`expect.verified = True`
        # on a proposed clause, or `expect.source = "proposed"` on a verified one). Both
        # fields live directly on this model, so assignment to either alone is enough to
        # retrigger this check; there is no nested sub-model to lose track of the way
        # `Node.state.protected` is.
        #
        # It deliberately does NOT cover two bypasses: `Expect.model_construct(...)` and
        # `expect.model_copy(update={...})` -- both skip validation entirely by Pydantic's
        # design. An `Expect` built or copied through either can end up proposed-and-
        # verified with no error raised. Anything reaching for `model_copy(update=...)` on
        # an `Expect` -- an overlay's `extend_expects` merge is the obvious candidate --
        # must not assume this validator will catch a mistake there.
        if self.source == "proposed" and self.verified:
            raise ValueError("a proposed expect cannot be verified")
        return self


class Step(BaseModel):
    """One replayable action.

    `id` is a plain string; step ordering comes from list position on
    `Artifact.steps`, never from `id`.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    action: ActionKind
    target: Target | None = None
    locator: Locator | None = None
    value: StepValue | None = None
    risk: Risk | None = None
    expects: list[Expect] = Field(default_factory=list)
    extract: Literal["text", "value", "attribute"] | None = None
    parse: Literal["raw", "money", "int", "date"] | None = None
    into: str | None = None


class InputSpec(BaseModel):
    """A JSON-Schema-shaped description of one declared capability input.

    S4.2 decision 5: `inputs`/`outputs` are JSON Schema generated from Pydantic, so the
    artifact doubles as a tool definition with no second schema to drift from.
    """

    model_config = ConfigDict(extra="forbid")

    type: str
    # The one deliberate regular expression allowed anywhere in an artifact: a JSON Schema
    # validation pattern for a caller's argument, never a locator.
    pattern: str | None = None
    required: bool = True
    # S4.2 decision 6: declared, not inferred. Redaction driven by guessing at PII is a
    # leak waiting to happen, so this defaults to False and must be stated explicitly.
    sensitive: bool = False


class OutputSpec(BaseModel):
    """A JSON-Schema-shaped description of one declared capability output."""

    model_config = ConfigDict(extra="forbid")

    type: str
    format: str | None = None
    # S4.2 decision 6: declared, not inferred.
    redact: bool = False


class App(BaseModel):
    """Identifies the application and entry point a capability targets -- the product,
    never the tenant (S4.2 decision 4).
    """

    model_config = ConfigDict(extra="forbid")

    vendor_product: str
    variant: str
    surface: str
    entry: str


class Settle(BaseModel):
    """How long, and how often, to wait for the surface to settle after an action."""

    model_config = ConfigDict(extra="forbid")

    timeout_ms: int
    poll_ms: int


class Success(BaseModel):
    """The observation that marks the whole capability as having succeeded."""

    model_config = ConfigDict(extra="forbid")

    checkpoint: Matcher


class Recovery(BaseModel):
    """A globally recoverable condition, detected and handled independently of any single
    step's `expects`.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    detect: Matcher
    # S4.2 decision 2: `escalate` is a first-class handler. Session expiry, for example, is
    # not auto-recovered -- silently re-authenticating a banking session is exactly the
    # class of action that should require a person, and the schema states that rather than
    # omitting it.
    handle: Literal["dismiss", "escalate"]


class Provenance(BaseModel):
    """Where a capability came from, recorded so the artifact is reviewable without the
    raw model transcript.
    """

    model_config = ConfigDict(extra="forbid")

    discovered_at: datetime
    model: str
    policy_mode: str
    provider_retention: str
    run_id: str
    # S4.2 decision 7: a pointer, not the transcript itself. The artifact is reviewable
    # without it, and the transcript is retrievable when debugging.
    trace_ref: str


class CapabilityPolicy(BaseModel):
    """A per-capability deployment allowlist.

    E4: declared here; Task 2 is what enforces it (load-time validation narrows rather
    than widens a tenant overlay's deployment allowlist, S4.4). Both narrow an
    otherwise-unconstrained deployment, so "not stated" means "not narrowed", not
    "narrowed to nothing" -- hence `None`, not an empty list, as each field's default.

    Two narrowing axes, paths and actions, not three: origin is deliberately absent,
    because §6.1 keeps the allowlist "configuration, never the artifact" and §4.2
    decision 4 rejects storing a host in the artifact at all, so narrowing on origin --
    which needs a hostname to compare against -- cannot be expressed here (E25).
    """

    model_config = ConfigDict(extra="forbid")

    allowed_paths: list[str] | None = None
    allowed_actions: list[ActionKind] | None = None


class Artifact(BaseModel):
    """The capability contract: S4.1's on-disk shape, field for field.

    Deliberately absent: `status` and `stability`. S4.1 is explicit that lifecycle state
    lives in `artifacts/registry.json`, keyed by `(id, version)`; the artifact file at
    `artifacts/<id>/v<version>.yaml` is immutable, so lifecycle state cannot live in it.
    """

    model_config = ConfigDict(extra="forbid")

    # Governs how to parse this file.
    schema_version: int
    id: str
    # S4.2 decision 3: `version` is the capability revision; `schema_version` above is a
    # separate number that governs parsing. Conflating the two is a migration trap.
    version: int
    name: str
    description: str
    # Set only by compile-time self-verification (a later task); this model just carries
    # the field.
    verified: bool
    app: App
    settle: Settle
    max_duration_ms: int
    inputs: dict[IdentifierKey, InputSpec] = Field(default_factory=dict)
    outputs: dict[IdentifierKey, OutputSpec] = Field(default_factory=dict)
    steps: list[Step]
    success: Success
    recovery: list[Recovery] = Field(default_factory=list)
    provenance: Provenance
    # `None` means no `policy` block was declared at all; `CapabilityPolicy()` means one
    # was declared and left empty. `Field(default_factory=CapabilityPolicy)` collapsed
    # both into the same all-`None` state, which is exactly the distinction E4's narrowing
    # check needs to draw -- it must tell "not checked" from "checked and clean" apart, and
    # can only do that if "not declared" survives parsing as a different value than
    # "declared empty".
    policy: CapabilityPolicy | None = None


class LocatorOverride(BaseModel):
    """The two locator fields a tenant overlay may change (S4.3): a control's accessible
    `name`, or the `surface_path` it is reached through. Everything else about the base
    locator -- its strategy, its rationale, its confidence -- is not overlay territory.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    surface_path: list[SurfaceSegment] | None = None


class Overlay(BaseModel):
    """A tenant variant, resolved onto a base artifact at load time (S4.3).

    Keyed onto the base artifact by `base_id`/`base_version`; `targets` names the app
    variant (S4.2 decision 4's `app.variant`, e.g. `tenant_acme`) this overlay applies to.
    An overlay may override a locator's `name` or `surface_path` (`locator_overrides`,
    keyed by step id), insert new steps after an existing one (`insert_after`, keyed by
    the step id they follow), skip a step (`skip_steps`), and extend a step's `expects`
    (`extend_expects`, keyed by step id) -- and nothing else.

    Task 3 revision: `inputs`/`outputs` below were absent from Task 1's version of this
    model, on the reasoning that `extra="forbid"` already made `Overlay(inputs=...)`
    structurally impossible, so S4.3's fifth condition needed no runtime check. Task 3's
    tests (`test_an_overlay_changing_inputs_is_rejected`,
    `test_an_overlay_changing_outputs_is_rejected`) require the opposite: an overlay that
    attempts to change the contract must construct successfully and be caught by
    `cua.artifact.overlay.validate_overlay` as `OVERLAY_CHANGES_CONTRACT`. That is also the
    literal wording of S4.3 -- "it is rejected by the validator rather than merged" -- which
    names the validator, not the parser, as the enforcement point. So this model now
    declares both fields (defaulting to `None`, meaning "not attempted") purely so the
    validator has something to see and reject; nothing in `cua/artifact/` ever merges a
    non-`None` value here into a resolved artifact. This supersedes Task 1's two
    `test_an_overlay_cannot_declare_inputs`/`test_an_overlay_cannot_declare_outputs` tests
    in `tests/artifact/test_models.py`, updated alongside this change rather than left red.
    """

    model_config = ConfigDict(extra="forbid")

    targets: str
    base_id: str
    base_version: int
    # Established by self-verifying the resolved base-plus-overlay against the variant it
    # targets. A verified base artifact says nothing about whether the overlay resolves
    # correctly on top of it, so this defaults to unverified like `Expect.verified` does.
    verified: bool = False
    locator_overrides: dict[str, LocatorOverride] = Field(default_factory=dict)
    insert_after: dict[str, list[Step]] = Field(default_factory=dict)
    skip_steps: list[str] = Field(default_factory=list)
    extend_expects: dict[str, list[Expect]] = Field(default_factory=dict)
    # Present only so `validate_overlay` can reject an attempt to declare either -- see the
    # docstring above. `None` means "the overlay file did not try"; a non-`None` value is
    # always an error and is never read by `resolve_overlay`.
    inputs: dict[IdentifierKey, InputSpec] | None = None
    outputs: dict[IdentifierKey, OutputSpec] | None = None
