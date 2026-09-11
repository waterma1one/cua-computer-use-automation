"""The capability contract vocabulary: pure Pydantic models, no I/O.

Mirrors spec S4.1's on-disk artifact shape field for field. This module is pure data --
no cross-field validation beyond what the nine decisions in spec S4.2 and the ambiguity
resolutions in the phase-3 task brief actually require, and (like `cua/surface/models.py`)
nothing here may import a browser driver, reference a DOM, or carry a CSS selector or an
XPath: `tests/test_architecture.py` enforces this over the whole `cua/artifact/` package.
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
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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

Risk = Literal["safe", "risky", "irreversible"]
Outcome = Literal["continue", "business", "retry", "fail"]
ExpectSource = Literal["observed", "proposed", "authored"]


class Expect(BaseModel):
    """One outcome clause attached to a step: "if the screen now matches `when`, the
    business outcome is `outcome`."
    """

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
        if self.source == "proposed" and self.verified:
            raise ValueError("a proposed expect cannot be verified")
        return self


class Step(BaseModel):
    """One replayable action.

    `id` is a plain string; step ordering comes from list position on
    `Artifact.steps`, never from `id`.
    """

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

    type: str
    format: str | None = None
    # S4.2 decision 6: declared, not inferred.
    redact: bool = False


class App(BaseModel):
    """Identifies the application and entry point a capability targets -- the product,
    never the tenant (S4.2 decision 4).
    """

    vendor_product: str
    variant: str
    surface: str
    entry: str


class Settle(BaseModel):
    """How long, and how often, to wait for the surface to settle after an action."""

    timeout_ms: int
    poll_ms: int


class Success(BaseModel):
    """The observation that marks the whole capability as having succeeded."""

    checkpoint: Matcher


class Recovery(BaseModel):
    """A globally recoverable condition, detected and handled independently of any single
    step's `expects`.
    """

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
    than widens a tenant overlay's deployment allowlist, S4.4). All three narrow an
    otherwise-unconstrained deployment, so "not stated" means "not narrowed", not
    "narrowed to nothing" -- hence `None`, not an empty list, as the default.
    """

    allowed_origins: list[str] | None = None
    allowed_paths: list[str] | None = None
    allowed_actions: list[ActionKind] | None = None


class Artifact(BaseModel):
    """The capability contract: S4.1's on-disk shape, field for field.

    Deliberately absent: `status` and `stability`. S4.1 is explicit that lifecycle state
    lives in `artifacts/registry.json`, keyed by `(id, version)`; the artifact file at
    `artifacts/<id>/v<version>.yaml` is immutable, so lifecycle state cannot live in it.
    """

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
    inputs: dict[str, InputSpec] = Field(default_factory=dict)
    outputs: dict[str, OutputSpec] = Field(default_factory=dict)
    steps: list[Step]
    success: Success
    recovery: list[Recovery] = Field(default_factory=list)
    provenance: Provenance
    policy: CapabilityPolicy = Field(default_factory=CapabilityPolicy)


class LocatorOverride(BaseModel):
    """The two locator fields a tenant overlay may change (S4.3): a control's accessible
    `name`, or the `surface_path` it is reached through. Everything else about the base
    locator -- its strategy, its rationale, its confidence -- is not overlay territory.
    """

    name: str | None = None
    surface_path: list[SurfaceSegment] | None = None


class Overlay(BaseModel):
    """A tenant variant, resolved onto a base artifact at load time (S4.3).

    Keyed onto the base artifact by `base_id`/`base_version`; `targets` names the app
    variant (S4.2 decision 4's `app.variant`, e.g. `tenant_acme`) this overlay applies to.
    An overlay may override a locator's `name` or `surface_path` (`locator_overrides`,
    keyed by step id), insert new steps after an existing one (`insert_after`, keyed by
    the step id they follow), skip a step (`skip_steps`), and extend a step's `expects`
    (`extend_expects`, keyed by step id) -- and nothing else: S4.3 forbids an overlay from
    touching `inputs` or `outputs`, which would silently break every caller, and that
    prohibition is exactly why this model declares no such field to touch. Enforcing the
    prohibition, and resolving an overlay onto its base, is Task 3's job; this model only
    has to be able to represent one.
    """

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
