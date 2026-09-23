"""The deterministic replay step loop: turns an `Artifact` plus caller-supplied `inputs`
into exactly one `Success | BusinessOutcome | Failure` (`cua.replay.result.ReplayResult`),
acting on the target application only through the `Surface` protocol (D19).

No Playwright, no Selenium, no DOM concept, no CSS selector or XPath anywhere in this
module -- `tests/test_architecture.py` greps `cua/replay/` for exactly that, and this
module additionally never imports `cua.observability`: `EvidenceSink` below is a local
`Protocol` so this module can be written, tested and reviewed independently of how a
run's evidence is actually persisted.

Every wait comes from the injected `Clock` (default `cua.replay.settle.SYSTEM_CLOCK`),
never a fixed `sleep` -- `settle()` already enforces this for the per-step poll loop, and
this module never sleeps on its own.

Control-flow rulings this module implements, restated briefly (the task brief and the
fix-round rulings carry the full reasoning): E9/E27 (an irreversible step needs both
`confirm_irreversible` and an `idempotency_key`; the key is burned immediately before
that step's own `act`, scoped to the artifact's `(id, version)`), E10/E22
(`validate_inputs` as its own public function), E14 (an `ok=False` action result with a
pending dialog falls through to `settle()`; without one it is `PRECONDITION_FAILED`), E17
(a `fail`/`business` expect with a malformed code is refused before the surface is ever
touched), E18 (the artifact's `success.checkpoint` is itself settled, as a synthetic step,
after the last real step), E21 (a frame is captured only for a failure that arises during
or after a surface interaction -- every pre-loop gate and every pre-act refusal passes
`capture=False` and never calls a `Surface` method at all), E28 (`replay()` returns
exactly one of the three result shapes; every `SurfaceError`, wherever it arises, is a
`SESSION_LOST` unless the surface has recorded an allowlist violation (E3, below), and
`_fail` itself is safe on a dead surface), E29 (the failure-kind vocabulary, the
expect-code check and the path allowlist rule each have one home in `cua.artifact`; this
module calls them rather than carrying copies).

Phase 5: E3 (`_violation` checks `Surface.allowlist_violation()` after every `act`, after
every step's settle, and after the checkpoint settle -- ahead of any other reading of what
the surface returned or raised), E5 (`_policy_gate` checks every step's action type and
risk through `check_action` before any step runs, so a replay is never refused partway
through at step N), E6 (the deployment allowlist is narrowed by the artifact's own
`policy` before the gate ever sees it, whether or not the validator ran over this
artifact), E9 (a `bound` trace event carries the extracted value and the output's
`redact` flag, never an action's own value), E10 (`settle()`'s `record_dialog` reports
every dialog it handles, dismissed or not, to the trace).

Phase 6: E2 (`Escalator` is a structural `Protocol`; this module still never imports
`cua.session` -- the dependency runs the other way, the same shape `EvidenceSink` already
proved out for `cua.observability`), E5 (three triggers route a `Failure` through
`escalator.escalate(...)` instead of ending the replay: a settle timeout
(`NO_BRANCH_MATCHED`), a recovery rule's `Escalate` outcome (`ESCALATION_UNAVAILABLE`), and
any `Failure` on an `irreversible` step, whatever its own kind -- but never an
`ALLOWLIST_VIOLATION`, D40's override, unconditionally), E6 (a `Resolved` handback
re-verifies the resume checkpoint and re-resolves the next step's locator before
continuing, rather than re-running the step that paused).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from cua.artifact.models import (
    Artifact,
    Expect,
    FailureKind,
    FromInput,
    InputSpec,
    LiteralValue,
    Matcher,
    RegistryStatus,
    Step,
)
from cua.artifact.validate import DeploymentAllowlist, expect_code_problem
from cua.policy.allowlist import check_action
from cua.replay.extract import ParseError, extract
from cua.replay.result import (
    BusinessOutcome,
    CannotResolve,
    Failure,
    HandbackOutcome,
    Mode,
    ReplayResult,
    ResolvedManually,
    RestartFrom,
    Success,
    mint_run_id,
)
from cua.replay.settle import (
    SYSTEM_CLOCK,
    BranchOutcome,
    Business,
    Clock,
    Continue,
    DialogUnhandled,
    Escalate,
    Fail,
    Violated,
    settle,
)
from cua.surface.base import Surface, SurfaceError
from cua.surface.models import Action, EvidenceFrame, Locator, Node, Observation, Resolution

__all__ = ["Escalator", "EvidenceSink", "replay", "validate_inputs"]

# E27: an irreversible step, once actually attempted under a given idempotency key, must
# not be attempted again under the same key for the same capability -- in-memory, for the
# lifetime of this process. Keyed by `(artifact.id, artifact.version, key)` so two
# unrelated capabilities reusing one key string never collide. Phase 9's console or a
# durable store is a later phase's job; this is the minimal control that keeps a duplicate
# "post" from ever reaching the surface twice.
_IdempotencyScope = tuple[str, int, str]
_used_idempotency_keys: set[_IdempotencyScope] = set()


def _reset_idempotency_keys() -> None:
    """Test seam (E27): empties the process-lifetime seen-set so one test's burned key can
    never refuse another test's replay. `tests/replay/conftest.py` calls it around every
    test; production code never does.
    """
    _used_idempotency_keys.clear()


@runtime_checkable
class EvidenceSink(Protocol):
    """What the engine needs from an evidence writer, and nothing more.

    `cua.observability.EvidenceWriter` (phase 5, not built yet) satisfies this shape;
    this module never imports that package, so it is fully testable today against the
    `FakeEvidenceSink` in `tests/replay/test_engine.py` and the `_NullEvidenceSink` below.
    """

    run_id: str

    def event(self, **fields: object) -> None:
        """Records a structured trace event. Fields are caller-defined; this protocol
        does not constrain their shape.
        """
        ...

    def frame(self, frame: EvidenceFrame, name: str) -> None:
        """Records one evidence frame under a human-readable name (typically a step id,
        or `"input"` for a failure that arose before any step ran).
        """
        ...

    def evidence_ref(self) -> str:
        """A pointer into this run's evidence trail, stored on every `ReplayResult`."""
        ...


_ESCALATION_KINDS: frozenset[FailureKind] = frozenset(
    {"NO_BRANCH_MATCHED", "ESCALATION_UNAVAILABLE"}
)
# E5: trigger (b) is a settle timeout; trigger (a) is a recovery rule's Escalate outcome,
# which _translate_outcome (UNCHANGED by this task) already turns into a Failure with this
# exact kind, D31's own shipped mapping. Trigger (c) (any Failure on an irreversible step)
# is checked separately in _maybe_escalate, not through this set.


@runtime_checkable
class Escalator(Protocol):
    """What the engine needs to hand an escalation to a human, and nothing more (E2).

    A pure interface -- `cua.session.service.SessionService` satisfies it structurally; this
    module never imports `cua.session` (the same one-way coupling `EvidenceSink` already
    proved out for `cua.observability`). Fully testable today against `FakeEscalator` in
    `tests/replay/test_engine.py`.
    """

    def escalate(
        self, *, step_id: str | None, kind: FailureKind, expected: str, observed: str,
    ) -> HandbackOutcome:
        """Blocks until a human hands the intervention back (or it expires -- a caller's own
        `escalate()` implementation is responsible for turning an expiry into whichever
        `HandbackOutcome` it prefers to model that as, or for raising in a way `replay()`'s
        caller can translate to `ESCALATION_TIMEOUT`; this protocol does not constrain that
        choice)."""
        ...


@dataclass
class _NullEvidenceSink:
    """Stands in for `evidence=None`: every write is a no-op, but `evidence_ref()` still
    returns a real, shaped pointer, because `Success`/`BusinessOutcome`/`Failure` all
    require one whether or not a caller wants the evidence actually written anywhere.
    """

    run_id: str = field(default_factory=mint_run_id)

    def event(self, **fields: object) -> None:
        pass

    def frame(self, frame: EvidenceFrame, name: str) -> None:
        pass

    def evidence_ref(self) -> str:
        return f"evidence/{self.run_id}"


_REDACTION_MARKER = "[REDACTED]"


def _shown(value: object, spec: InputSpec) -> str:
    """How an input's value is rendered into a `Failure` (E31): `repr(value)` for an
    ordinary input, the literal `[REDACTED]` for one declared `sensitive`."""
    return _REDACTION_MARKER if spec.sensitive else repr(value)


def _mask_sensitive_inputs(run: _Run, text: str) -> str:
    """`text` with every `sensitive` input's literal value replaced by `[REDACTED]`.

    E31 covers text this module *composes*, where the value and its `InputSpec` are in
    hand (`_shown`). This covers text this module *receives* -- a surface's violation
    reason, built around a URL whose query string may carry whatever was typed into a GET
    form. Only a declared value can be recognised, so the check is a plain substring
    replace over the declared sensitive values.

    The same trade-off `cua.surface.snapshot.scrub_protected_values` states: a global
    textual replace cannot tell the credential from unrelated text that happens to contain
    it, so an ordinary word colliding with a short password is masked too. A visibly
    redacted sentence is the acceptable half of that; a credential written to evidence is
    not. An empty value matches everywhere and is skipped.
    """
    for name, spec in run.artifact.inputs.items():
        if not spec.sensitive or name not in run.inputs:
            continue
        value = str(run.inputs[name])
        if value and value in text:
            text = text.replace(value, _REDACTION_MARKER)
    return text


def validate_inputs(artifact: Artifact, inputs: dict[str, object]) -> Failure | None:
    """E10/E22: checks `inputs` against `artifact.inputs` and returns an `INVALID_INPUT`
    `Failure`, or `None` if every declared input is satisfied.

    A standalone function with no dependency on anything else in this module -- it never
    touches a `Surface`, because it must be callable before one exists (a CLI validates
    inputs before ever launching a browser). `replay()` calls this same function as its
    own first real check, so there is one implementation of the rule, not two.

    Stated limit (E10): only the `"string"` and `"integer"` declared `InputSpec.type`
    values are checked here; any other declared type passes through unvalidated. A
    `"string"` input additionally checks `InputSpec.pattern`, when declared, as a full-
    string regular expression match. `required` is enforced for every declared type.

    E31: when the input's `InputSpec.sensitive` is `True`, the value itself never enters
    `expected` or `observed` -- `[REDACTED]` stands in for it. The input's name, its
    declared pattern and the Python type name of what arrived are not the value and
    stay, so the failure is still diagnosable. The rule lives here, at the construction
    site, because nothing downstream (`write_result`, a CLI echo) knows which input a
    finished `Failure`'s text came from.
    """
    sink = _NullEvidenceSink()
    for name, spec in artifact.inputs.items():
        if name not in inputs:
            if spec.required:
                return Failure(
                    kind="INVALID_INPUT", step_id=None,
                    expected=f"input {name!r} is provided",
                    observed=f"input {name!r} was not provided",
                    evidence_ref=sink.evidence_ref(),
                )
            continue

        value = inputs[name]
        if spec.type == "string":
            if not isinstance(value, str):
                return Failure(
                    kind="INVALID_INPUT", step_id=None,
                    expected=f"input {name!r} is a string",
                    observed=(f"input {name!r} was {_shown(value, spec)} "
                              f"({type(value).__name__})"),
                    evidence_ref=sink.evidence_ref(),
                )
            if spec.pattern is not None and re.fullmatch(spec.pattern, value) is None:
                return Failure(
                    kind="INVALID_INPUT", step_id=None,
                    expected=f"input {name!r} matches pattern {spec.pattern!r}",
                    observed=f"input {name!r} was {_shown(value, spec)}",
                    evidence_ref=sink.evidence_ref(),
                )
        elif spec.type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                return Failure(
                    kind="INVALID_INPUT", step_id=None,
                    expected=f"input {name!r} is an integer",
                    observed=(f"input {name!r} was {_shown(value, spec)} "
                              f"({type(value).__name__})"),
                    evidence_ref=sink.evidence_ref(),
                )
        # Any other declared `type` is not validated here -- the stated limit above.

    # E28: a step that reads an input which is not in `inputs` -- undeclared, or declared
    # optional and simply not supplied -- is refused here, pre-browser, rather than raising
    # a `KeyError` out of the step loop after the surface has been touched.
    for step in artifact.steps:
        if isinstance(step.value, FromInput) and step.value.from_input not in inputs:
            name = step.value.from_input
            return Failure(
                kind="INVALID_INPUT", step_id=step.id,
                expected=f"input {name!r}, which step {step.id!r} reads, is provided",
                observed=f"input {name!r} was not provided",
                evidence_ref=sink.evidence_ref(),
            )
    return None


def _fail(
    kind: FailureKind, step_id: str | None, expected: str, observed: str, *,
    surface: Surface, sink: EvidenceSink, capture: bool,
) -> Failure:
    """Builds every `Failure` the step loop returns.

    E21: `capture` draws the line at "has `resolve`/`act`/`observe` been called yet for
    this failure's step". When `True`, `surface.pending_dialog()` is checked first -- a
    pending native dialog blocks a screenshot, so that case is recorded as a skipped
    capture (`sink.event(...)`) rather than silently producing no frame with no
    explanation. Otherwise `surface.capture()` is attempted. When `capture` is `False`,
    `surface` is never touched at all -- what every `PoisonSurface`-based test in this
    task's suite relies on.

    E28: safe on a dead surface. Both calls sit inside the one `try`, so a
    `pending_dialog()` or `capture()` that raises `SurfaceError` records why no frame
    exists and still returns the `Failure` of the original kind -- a failed capture must
    never hide the failure it was trying to illustrate, and must never escape `replay()`.
    """
    if capture:
        try:
            message = surface.pending_dialog()
            if message is not None:
                sink.event(step_id=step_id, kind="capture_skipped",
                           reason="a native dialog is pending")
            else:
                sink.frame(surface.capture(), step_id or "input")
        except SurfaceError as exc:
            sink.event(step_id=step_id, kind="capture_failed", reason=str(exc))
    return Failure(kind=kind, step_id=step_id, expected=expected, observed=observed,
                    evidence_ref=sink.evidence_ref())


def _violation(run: _Run, step_id: str | None) -> Failure | None:
    """E3: the surface's recorded allowlist violation, as the `Failure` it is, or `None`.
    Asked after every `act` and after every settle -- regardless of what settle returned
    or raised -- and after the checkpoint settle: a recorded violation wins over every
    other reading, including a settle outcome. A redirect onto a denied URL returns
    `ok=True` from `click`, an aborted `goto` raises, a `fail`/`continue` expect may
    happen to match the frozen page's unchanged content, and all of these must be
    `ALLOWLIST_VIOLATION`, never `PRECONDITION_FAILED`, `SESSION_LOST`, or some other
    branch outcome. `capture=True`: the surface has been touched, and a frozen surface
    still captures (Task 4). A surface that cannot even answer is treated as having no
    violation; the caller's own `SurfaceError` path then applies.

    Fix round 2: when `settle()` itself already discovered the violation (a `Violated`
    outcome, translated to an `ALLOWLIST_VIOLATION` `Failure` by `_translate_outcome`),
    the caller skips this call rather than asking again -- re-reading a sticky violation
    would only duplicate the `allowlist_violation` event and the captured frame that the
    first read already produced.

    E31, extended: the reason is a sentence the surface composed around a URL, and a URL
    carries a query string. A sensitive input filled into a GET form is therefore in the
    reason verbatim, and nothing downstream can recover it -- the evidence redaction pass
    matches PII *shapes*, and a password has no shape. So the values of this artifact's
    `sensitive` inputs are masked here, at the one place that still knows which string was
    a credential, before the reason reaches either the trace event or the `Failure`.
    """
    try:
        reason = run.surface.allowlist_violation()
    except SurfaceError:
        return None
    if reason is None:
        return None
    reason = _mask_sensitive_inputs(run, reason)
    run.sink.event(kind="allowlist_violation", step_id=step_id, reason=reason)
    return run.fail(
        "ALLOWLIST_VIOLATION", step_id,
        "every navigation the application initiates stays inside the deployment allowlist",
        reason, capture=True,
    )


def _describe_matcher(matcher: Matcher) -> str:
    role = f"role={matcher.role!r} " if matcher.role else ""
    return f"{matcher.strategy} {role}name={matcher.name!r} ({matcher.name_match})"


def _describe_expects(expects: list[Expect]) -> str:
    if not expects:
        return "the step to settle with nothing further declared"
    return "one of: " + "; ".join(f"{e.outcome} on {_describe_matcher(e.when)}" for e in expects)


def _continue_matchers(expects: list[Expect]) -> list[Matcher]:
    """Every `continue`-outcome `Expect.when` in `expects`, in declared order -- the one
    place this project decides "which matcher does a step's own continue clause name"
    (D28). `_describe_continues` (a `Failure.expected` sentence) and `_resume_after` (E6, a
    `Matcher` to settle against) both call this rather than each re-deriving the list.
    """
    return [e.when for e in expects if e.outcome == "continue"]


def _describe_continues(expects: list[Expect], checkpoint: Matcher) -> str:
    """What a step *should* have settled on, for a matched `fail` clause's `expected`: its
    `continue` clauses, or the artifact's success checkpoint when it declares none. Naming
    the failure condition that matched there would restate `observed`.
    """
    continues = _continue_matchers(expects)
    if not continues:
        return f"the success checkpoint {_describe_matcher(checkpoint)}"
    return "one of: " + "; ".join(f"continue on {_describe_matcher(m)}" for m in continues)


def _describe_observation(observation: Observation) -> str:
    if not observation.nodes:
        return "no nodes were observed before the timeout"
    parts = [n.name or n.value or f"an unnamed {n.role}" for n in observation.nodes]
    return "observed: " + "; ".join(parts)


def _describe_locator(locator: Locator) -> str:
    role = f"role={locator.role!r} " if locator.role else ""
    return f"a unique control matching {locator.strategy} {role}name={locator.name!r}"


@dataclass
class _Run:
    """Everything one `replay()` call threads through its steps: the fixed inputs, the
    surface and sink, the deadline, and the bindings steps accumulate.

    `values` is keyed by `Step.into` (declared outputs and underscore-prefixed locals);
    `bound_by_step` holds the same extracted values keyed by the producing step's `id`,
    which is what a `from_step` reference names (`cua.artifact.validate._from_step_findings`
    resolves the reference graph by step id, and the engine must agree with it).
    """

    artifact: Artifact
    inputs: dict[str, object]
    surface: Surface
    sink: EvidenceSink
    clock: Clock
    deadline: int
    deployment: DeploymentAllowlist | None
    idempotency_key: str | None
    mode: Mode
    values: dict[str, object] = field(default_factory=dict)
    bound_by_step: dict[str, object] = field(default_factory=dict)
    steps_run: list[str] = field(default_factory=list)
    # Fix round 1, Important #2: whether the step currently being attempted has reached
    # `surface.act(...)` yet. Reset to `False` at the top of every `_run_step_inner` call and
    # set `True` once `_prepare_action` has succeeded for it (the same "point of no return"
    # `_run_step_inner` already burns the idempotency key at) -- so a `Failure` returned
    # before that point (`_prepare_action`'s own `LOCATOR_NOT_FOUND`/`AMBIGUOUS_LOCATOR`/
    # `PRECONDITION_FAILED`/etc.) is distinguishable, at the point it escalates, from one
    # `act()` had already been attempted for.
    step_acted: bool = False

    def fail(
        self, kind: FailureKind, step_id: str | None, expected: str, observed: str, *,
        capture: bool,
    ) -> Failure:
        return _fail(kind, step_id, expected, observed,
                     surface=self.surface, sink=self.sink, capture=capture)


def _policy_gate(
    artifact: Artifact, deployment: DeploymentAllowlist | None, status: RegistryStatus,
    confirm_irreversible: bool, idempotency_key: str | None, sink: EvidenceSink,
) -> Failure | None:
    """The pre-loop refusals that need no surface (E17, E5/E6, E9/E27), every one
    constructed directly rather than through `_fail` -- nothing has touched the surface
    yet, so there is nothing to capture.

    E17: `load()` already refuses to persist an artifact whose `fail` expect names an
    unknown code or whose `business` expect names none, but this module must not trust
    that every artifact it is handed went through `load()`. `expect_code_problem` is the
    validator's own check (E29), not a copy of it.

    E5/E6: every step's action type is checked against `deployment` and its risk against
    `status` before any step runs, so a replay never gets partway through before being
    refused at step N. `deployment` here is already narrowed by the artifact's own policy
    (E6, `DeploymentAllowlist.narrowed_by`) -- this function never narrows it itself.

    E27: an irreversible step requires both `confirm_irreversible` and an
    `idempotency_key`, and a key already burned for this `(id, version)` is refused here,
    before the browser is touched. The burn itself happens in `_run_step`, immediately
    before that step's own `act`.
    """
    for step in artifact.steps:
        for expect in step.expects:
            problem = expect_code_problem(expect)
            if problem is not None:
                return Failure(
                    kind="POLICY_BLOCKED", step_id=step.id,
                    expected="every fail expect names a FailureKind and every business "
                             "expect names a code",
                    observed=(
                        f"step {step.id!r} has a {expect.outcome} expect with code "
                        f"{expect.code!r} ({problem})"
                    ),
                    evidence_ref=sink.evidence_ref(),
                )

    # E5 (phase 5): every step is checked before any runs -- the action type against the
    # effective allowlist (`ALLOWLIST_VIOLATION`), the risk against the registry status
    # (`POLICY_BLOCKED`) -- so no session is created for a replay that would be refused at
    # step N (§5.4's spirit, E21's capture=False). `check_action` is the one implementation;
    # `deployment` here is already narrowed by the artifact's own policy (E6). The E18
    # checkpoint step is not in `artifact.steps` and performs no act, so it is not gated.
    for step in artifact.steps:
        decision = check_action(step.action, step.risk, status, deployment)
        if not decision.allowed:
            assert decision.kind is not None
            return Failure(
                kind=decision.kind, step_id=step.id,
                expected="every step is permitted by the deployment allowlist and may run "
                         f"unattended at status {status!r}",
                observed=f"step {step.id!r}: {decision.reason}",
                evidence_ref=sink.evidence_ref(),
            )

    irreversible = next((s for s in artifact.steps if s.risk == "irreversible"), None)
    if irreversible is None:
        return None
    if not confirm_irreversible:
        return Failure(
            kind="POLICY_BLOCKED", step_id=irreversible.id,
            expected="an irreversible step requires confirm_irreversible=True",
            observed=(
                f"step {irreversible.id!r} is irreversible and confirm_irreversible was not set"
            ),
            evidence_ref=sink.evidence_ref(),
        )
    if idempotency_key is None:
        return Failure(
            kind="POLICY_BLOCKED", step_id=irreversible.id,
            expected="an irreversible step requires an idempotency_key",
            observed=f"step {irreversible.id!r} is irreversible and no idempotency_key was given",
            evidence_ref=sink.evidence_ref(),
        )
    if (artifact.id, artifact.version, idempotency_key) in _used_idempotency_keys:
        return Failure(
            kind="POLICY_BLOCKED", step_id=irreversible.id,
            expected="idempotency_key names an operation not already attempted",
            observed=(
                f"idempotency_key {idempotency_key!r} was already used to attempt "
                f"{artifact.id!r} v{artifact.version}"
            ),
            evidence_ref=sink.evidence_ref(),
        )
    return None


def _step_value(run: _Run, step: Step) -> str | None | Failure:
    """Resolves `step.value` to the plain string an `Action` carries -- `None` when the
    step carries no value -- or the `Failure` that refuses it. Total: never raises.

    E28: a `from_step` naming a step that bound nothing is `PRECONDITION_FAILED` with a
    frame (the surface was just asked to resolve this step's locator). A `from_input`
    naming an absent input was already refused pre-browser by `validate_inputs`; the
    re-check here only keeps this function total should a caller skip that gate.
    """
    value = step.value
    if value is None:
        return None
    if isinstance(value, LiteralValue):
        return value.literal
    if isinstance(value, FromInput):
        if value.from_input not in run.inputs:
            return run.fail(
                "INVALID_INPUT", step.id,
                f"input {value.from_input!r}, which step {step.id!r} reads, is provided",
                f"input {value.from_input!r} was not provided", capture=True,
            )
        return str(run.inputs[value.from_input])
    if value.from_step not in run.bound_by_step:  # FromStep: the only StepValue arm left
        return run.fail(
            "PRECONDITION_FAILED", step.id,
            f"step {value.from_step!r} binds a value before step {step.id!r} reads it",
            f"step {value.from_step!r} bound nothing", capture=True,
        )
    return str(run.bound_by_step[value.from_step])


def _resolution_failure_parts(
    resolution: Resolution, locator: Locator,
) -> tuple[FailureKind, str, str]:
    """The `(kind, expected, observed)` a non-`unique` `Resolution` translates to -- shared by
    `_prepare_action` (wraps it in `run.fail(..., capture=True)`) and `_resume_after` (hands it
    to `_escalate_and_continue` instead), so there is one mapping from `Resolution.kind` to
    `FailureKind`, not two (D28)."""
    if resolution.kind == "not_found":
        return "LOCATOR_NOT_FOUND", _describe_locator(locator), resolution.reason
    if resolution.kind == "ambiguous":
        return ("AMBIGUOUS_LOCATOR",
                _describe_locator(locator) + " to resolve to exactly one control",
                f"{resolution.count} controls matched")
    # "precondition_failed" is the only Resolution.kind left besides "unique".
    assert resolution.kind == "precondition_failed"
    return ("PRECONDITION_FAILED", f"the control is {resolution.which}",
            f"the control failed the {resolution.which} precondition")


def _prepare_action(run: _Run, step: Step) -> tuple[Action, Node | None] | Failure:
    """Turns one step into the `Action` to perform, resolving its locator and value first,
    or returns the `Failure` that stops it before anything is performed.

    `navigate` (E8/E29): the declared `target.path` is checked against the deployment's
    `permits_path` before `act` is ever called -- `capture=False`, since the navigation
    this check exists to prevent has, by construction, not happened. A `navigate` with no
    target is `PRECONDITION_FAILED`, never an `Action` carrying an empty path (E28). Live
    enforcement (redirects, a page that navigates itself) is the surface's
    (`allowlist_violation`), checked after `act` and after settle.

    Every other action: `resolve` first, translating `NotFound`/`Ambiguous`/
    `PreconditionFailed` directly (`capture=True` -- the surface was just asked), then the
    value. Returns the resolved `Node` alongside the action so extraction reads from it.

    E3 applies to `resolve` exactly as it applies to `act` and to settle: `resolve` is a
    blocking surface call, so a violation the surface recorded while it ran wins over
    every one of the four resolution failures. A frozen page resolves nothing -- reporting
    that as `LOCATOR_NOT_FOUND` would name the symptom and bury the cause.
    """
    if step.action == "navigate":
        if step.target is None:
            return run.fail(
                "PRECONDITION_FAILED", step.id, "a navigate step declares a target path",
                f"step {step.id!r} is a navigate with no target", capture=False,
            )
        path = step.target.path
        if run.deployment is not None and not run.deployment.permits_path(path):
            # Minor 6: the same rule explained the same way at both layers. A denied path
            # and a path nobody allowed are different refusals, and
            # `cua.policy.allowlist.check_navigation` already says which is which for the
            # live case; the static case must not invent a third sentence for it.
            denied = run.deployment.denying_prefix(path)
            if denied is not None:
                observed = (f"path {path!r} is denied by prefix {denied!r} "
                            "(deny rules are evaluated first and win)")
            else:
                observed = (f"path {path!r} is outside every allowed prefix "
                            f"{run.deployment.allowed_paths!r}")
            return run.fail(
                "ALLOWLIST_VIOLATION", step.id,
                "the navigate target is permitted by the deployment allowlist",
                observed, capture=False,
            )
        return Action(kind="navigate", locator=None, value=path), None

    resolved_node: Node | None = None
    if step.locator is not None:
        try:
            resolution = run.surface.resolve(step.locator)
        except SurfaceError as exc:
            violated = _violation(run, step.id)
            if violated is not None:
                return violated
            return run.fail(
                "SESSION_LOST", step.id, "the surface resolves the locator without error",
                str(exc), capture=True,
            )
        if resolution.kind != "unique":
            # Asked only on the failing branches: `_violation` records an event and a
            # frame, so asking on the success path would either report a violation the
            # post-`act` check is about to report anyway, or emit an event for nothing.
            violated = _violation(run, step.id)
            if violated is not None:
                return violated
            kind, expected, observed = _resolution_failure_parts(resolution, step.locator)
            return run.fail(kind, step.id, expected, observed, capture=True)
        resolved_node = resolution.node

    value = _step_value(run, step)
    if isinstance(value, Failure):
        return value
    return Action(kind=step.action, locator=step.locator, value=value), resolved_node


def _extract_binding(run: _Run, step: Step, resolved_node: Node | None) -> Failure | None:
    """Reads the step's declared extraction off the resolved node and binds it under the
    step's `id` (for `from_step`) and its `into` (for outputs and locals). A `ParseError` is
    `OUTPUT_VALIDATION_FAILED` -- a value that does not parse is never bound as `None`.

    A step that declares an extraction but resolved no node (no locator) binds nothing;
    the unbound-output check at the end of `replay()` reports that (E28) rather than this
    step guessing at what it should have read.
    """
    wants_extraction = (
        step.into is not None or step.extract is not None or step.parse is not None
    )
    if not wants_extraction or resolved_node is None:
        return None
    try:
        extracted = extract(resolved_node, step.extract or "text", step.parse)
    except ParseError as exc:
        return run.fail(
            "OUTPUT_VALIDATION_FAILED", step.id, f"a value parseable as {step.parse or 'text'}",
            str(exc), capture=True,
        )
    run.bound_by_step[step.id] = extracted
    if step.into is not None:
        run.values[step.into] = extracted
    redact = step.into in run.artifact.outputs and run.artifact.outputs[step.into].redact
    run.sink.event(kind="bound", step_id=step.id, into=step.into, value=extracted, redact=redact)
    return None


def _translate_outcome(
    run: _Run, outcome: BranchOutcome, step_id: str | None, expects: list[Expect],
) -> ReplayResult | None:
    """Translates one `settle()` result into the `ReplayResult` the caller (a step, or the
    post-loop checkpoint settle of E18) should return -- or `None` for `Continue`, meaning
    "proceed". Shared between the per-step call site and the checkpoint call site so there
    is exactly one mapping from `BranchOutcome` to `ReplayResult`, not two.

    Every branch other than `Continue` captures a frame (`capture=True`): `settle()`
    always calls `observe()` or `pending_dialog()` at least once before returning any of
    them, so by definition the surface has already been interacted with (E21).
    """
    if isinstance(outcome, Continue):
        return None
    if isinstance(outcome, Business):
        # E17/E28: `_policy_gate` refused every business expect without a code before the
        # loop began, and only a real step's own expects can produce `Business` (the
        # checkpoint step declares a single `continue`). Reaching here otherwise is an
        # invariant violation, checked explicitly rather than with `assert`.
        if step_id is None or outcome.code is None:
            raise RuntimeError(
                "a business outcome arose without a step id or code; the pre-loop "
                "expect-code gate did not run"
            )
        return BusinessOutcome(code=outcome.code, step_id=step_id, message=outcome.message,
                                evidence_ref=run.sink.evidence_ref())
    if isinstance(outcome, Fail):
        return run.fail(
            outcome.code, step_id,
            _describe_continues(expects, run.artifact.success.checkpoint),
            outcome.matched_text or "the matched fail condition carried no observable text",
            capture=True,
        )
    if isinstance(outcome, Escalate):
        return run.fail(
            "ESCALATION_UNAVAILABLE", step_id,
            "the triggering condition resolves without a human",
            (
                f"recovery rule {outcome.recovery_name!r} requires escalation, which an "
                "embedded replay cannot provide"
            ),
            capture=True,
        )
    if isinstance(outcome, DialogUnhandled):
        return run.fail(
            "UNHANDLED_DIALOG", step_id, "a recovery rule handles the pending dialog",
            f"an unhandled dialog appeared: {outcome.message!r}", capture=True,
        )
    if isinstance(outcome, Violated):
        # Route through the same `_violation` helper so the `allowlist_violation` event
        # and the capture happen exactly once, whether the violation is discovered here
        # or by a direct post-act/post-settle check. `_violation` re-reads the surface;
        # on the vanishingly unlikely chance it now reads `None` (the surface un-froze
        # between settle's read and this one), fall back to `outcome.reason` directly
        # rather than silently losing the violation `settle()` already reported.
        violated = _violation(run, step_id)
        if violated is not None:
            return violated
        return run.fail(
            "ALLOWLIST_VIOLATION", step_id,
            "every navigation the application initiates stays inside the deployment allowlist",
            _mask_sensitive_inputs(run, outcome.reason), capture=True,
        )
    # The only BranchOutcome variant left is TimedOut.
    return run.fail(
        "NO_BRANCH_MATCHED", step_id, _describe_expects(expects),
        _describe_observation(outcome.observation), capture=True,
    )


def _settle_step(run: _Run, step: Step, step_id: str | None) -> ReplayResult | None:
    """Settles one step (a real one, or the E18 checkpoint step with `step_id=None`) and
    translates the outcome; a `SurfaceError` raised from inside the poll loop checks
    `_violation` first (a frozen surface's own `dismiss_dialog` raises `SurfaceError` too)
    and only falls to `SESSION_LOST` (E28) when no violation was recorded.
    """
    try:
        outcome = settle(
            run.surface, step, run.artifact.settle, run.artifact.recovery, clock=run.clock,
            record_dialog=lambda message, handling: run.sink.event(
                kind="dialog", step_id=step_id, message=message, handling=handling),
        )
    except SurfaceError as exc:
        violated = _violation(run, step_id)
        if violated is not None:
            return violated
        return run.fail(
            "SESSION_LOST", step_id, "the surface stays observable while the step settles",
            str(exc), capture=True,
        )
    return _translate_outcome(run, outcome, step_id, step.expects)


def _run_step_inner(run: _Run, step: Step) -> ReplayResult | None:
    """Runs one step end to end: gates, action, extraction, settle. Returns the
    `ReplayResult` that ends the replay, or `None` to proceed to the next step.

    Gates in order: the deadline, `>=` not `>` (E25) -- `capture=False` while no step has
    yet completed, since nothing has touched the surface, `capture=True` once one has (the
    `risk is None` check that used to live here is now `_policy_gate`'s, via `check_action`,
    which runs before any step is attempted). Then `_prepare_action`; then, for an
    irreversible step, the idempotency key is burned (E27) immediately before `act` --
    nothing before this point can be a duplicate risk, so a replay that failed earlier may
    be retried under the same key, while one that reached this line may not, whatever
    happens next. `act` raising is `SESSION_LOST`; `ok=False` with a pending dialog falls
    through to `settle()` (E14) and without one is `PRECONDITION_FAILED`.

    Phase 6: this is the un-escalated step body -- exactly what `_run_step` used to be, in
    full, with none of its own D40 `_violation` checks touched. `_run_step` (below) wraps
    it and routes an escalation-worthy result through `_maybe_escalate` instead (E5).

    Fix round 1, Important #2: `run.step_acted` is reset here, at the top, for every
    attempt of this step, and set once `_prepare_action` has succeeded -- so a `Failure`
    from `_prepare_action` itself (before `act()` is ever reached) is marked pre-act, and
    `_maybe_escalate` can tell a human's `Resolved` "the state is fine, continue" apart from
    a case where there is no resulting state yet to confirm.
    """
    run.step_acted = False
    if run.clock.monotonic_ms() >= run.deadline:
        budget = run.artifact.max_duration_ms
        return run.fail(
            "DURATION_EXCEEDED", step.id, f"the replay completes within {budget}ms",
            f"the {budget}ms deadline was reached before step {step.id!r} began",
            capture=bool(run.steps_run),
        )

    prepared = _prepare_action(run, step)
    if isinstance(prepared, Failure):
        return prepared
    action, resolved_node = prepared
    run.step_acted = True

    if step.risk == "irreversible" and run.idempotency_key is not None:
        _used_idempotency_keys.add(
            (run.artifact.id, run.artifact.version, run.idempotency_key)
        )

    run.sink.event(kind="step_started", step_id=step.id, action=step.action, risk=step.risk)

    try:
        result = run.surface.act(action)
    except SurfaceError as exc:
        violated = _violation(run, step.id)
        if violated is not None:
            return violated
        return run.fail(
            "SESSION_LOST", step.id, "the surface completes the action without error",
            str(exc), capture=True,
        )

    violated = _violation(run, step.id)
    if violated is not None:
        return violated

    if not result.ok:
        try:
            dialog_message = run.surface.pending_dialog()
        except SurfaceError as exc:
            violated = _violation(run, step.id)
            if violated is not None:
                return violated
            return run.fail(
                "SESSION_LOST", step.id, "the surface reports its dialog state",
                str(exc), capture=True,
            )
        if dialog_message is None:
            return run.fail(
                "PRECONDITION_FAILED", step.id, "the action completes successfully",
                result.read_value or f"the {step.action} action did not complete", capture=True,
            )
        # E14: a pending dialog reaches the recovery machinery through settle() below,
        # instead of being reported here as a precondition failure.

    extraction_failure = _extract_binding(run, step, resolved_node)
    if extraction_failure is not None:
        return extraction_failure

    settled = _settle_step(run, step, step.id)
    if isinstance(settled, Failure) and settled.kind == "ALLOWLIST_VIOLATION":
        return settled  # settle already saw it and reported it once
    violated = _violation(run, step.id)
    if violated is not None:
        return violated
    if settled is not None:
        return settled

    run.steps_run.append(step.id)
    return None


def _run_step(
    run: _Run, artifact: Artifact, step_index: int, step: Step, escalator: Escalator | None,
) -> ReplayResult | None:
    outcome = _run_step_inner(run, step)
    return _maybe_escalate(run, artifact, step_index, step, outcome, escalator)


def _maybe_escalate(
    run: _Run, artifact: Artifact, step_index: int, step: Step,
    outcome: ReplayResult | None, escalator: Escalator | None,
) -> ReplayResult | None:
    """E5: routes an escalation-worthy Failure through `escalator.escalate(...)` instead of
    returning it directly. `None` (continue), a `BusinessOutcome`, and every `Failure` that
    does not qualify pass through unchanged -- including, always, an `ALLOWLIST_VIOLATION`
    (D40's override: a recorded violation is never escalated, whatever else is true of the
    step, review finding #19's residual risk).
    """
    if (escalator is None or run.mode != "supervised" or not isinstance(outcome, Failure)
            or outcome.kind == "ALLOWLIST_VIOLATION"):
        return outcome
    if outcome.kind in _ESCALATION_KINDS or step.risk == "irreversible":
        return _escalate_and_continue(
            run, artifact, step_id=step.id, step_index=step_index, escalator=escalator,
            kind=outcome.kind, expected=outcome.expected, observed=outcome.observed,
            pre_act=not run.step_acted,
        )
    return outcome


def _maybe_escalate_checkpoint(
    run: _Run, artifact: Artifact, ended: ReplayResult, escalator: Escalator | None,
) -> ReplayResult:
    """The checkpoint tail's own analogue of `_maybe_escalate` -- the synthetic `_checkpoint`
    step is never `irreversible` (trigger (c) does not apply to it), so only the
    `_ESCALATION_KINDS` check matters here. `_run_from`'s own caller has already returned an
    `ALLOWLIST_VIOLATION`-kind `ended` before this function is ever reached (D40), so no
    exclusion is needed here -- this function only ever sees a kind that could still qualify.
    """
    if escalator is None or run.mode != "supervised" or not isinstance(ended, Failure):
        return ended
    if ended.kind in _ESCALATION_KINDS:
        resumed = _escalate_and_continue(
            run, artifact, step_id=None, step_index=len(artifact.steps), escalator=escalator,
            kind=ended.kind, expected=ended.expected, observed=ended.observed,
        )
        # Unlike the per-step call site, there is no next loop iteration for `None` (the
        # checkpoint is the tail): every `_escalate_and_continue` branch reachable from the
        # tail bottoms out at a concrete `_run_from` call, a `Failure`, or a `Success` --
        # never a bare `None` -- so this holds structurally, not just by luck.
        assert resumed is not None
        return resumed
    return ended


def _escalate_and_continue(
    run: _Run, artifact: Artifact, *, step_id: str | None, step_index: int,
    escalator: Escalator, kind: FailureKind, expected: str, observed: str,
    pre_act: bool = False,
) -> ReplayResult | None:
    """`None` means "continue the loop from here" (used by `Resolved` after its own
    re-verification succeeds); any other return ends the whole replay. E17: the human-wait
    interval `escalator.escalate(...)` spends is excluded from `run.deadline`'s own budget --
    `run.deadline` is a plain mutable field on `_Run`, advanced by exactly however long the
    call took, so a resumed step has the same *remaining* budget it had when it paused.

    Fix round 1, Important #1 (D40): `escalate()` is the window where a human drives the
    live surface directly -- exactly the kind of blocking surface interaction every other
    call site in this file re-checks `_violation` after. A violation the human's own drive
    tripped must outrank whatever `HandbackOutcome` they returned, so it is checked here,
    once, immediately after `escalate()` returns and before any `isinstance` dispatch --
    never re-read afterward, the same pattern as every other `_violation(...)` site.

    Fix round 1, Important #2: `pre_act`, `True` only when the escalating step T's own
    `act()` was never reached (a pre-act `_prepare_action` failure on T itself, never the
    `_resume_after` T+1 probe, which passes its own escalations through with `pre_act`
    left at its default). `Resolved`'s meaning is "the human confirms the resulting state
    is fine, continue" -- there is no resulting state to confirm when `act()` never ran, so
    a `Resolved` handback in that case re-runs T instead (the same `_run_from(step_index)`
    path `RestartFrom(step_id=T)` already takes), rather than treating T as already done.
    """
    before = run.clock.monotonic_ms()
    outcome = escalator.escalate(step_id=step_id, kind=kind, expected=expected, observed=observed)
    run.deadline += run.clock.monotonic_ms() - before
    violated = _violation(run, step_id)
    if violated is not None:
        return violated
    if isinstance(outcome, CannotResolve):
        return run.fail(kind, step_id, expected, f"{observed} (operator: {outcome.note})",
                        capture=False)  # already captured once by the failure this wraps (E8)
    if isinstance(outcome, ResolvedManually):
        return Success(outputs={}, steps_run=run.steps_run, evidence_ref=run.sink.evidence_ref(),
                       assistance="human")  # E4: the EXISTING field, never a new one
    if isinstance(outcome, RestartFrom):
        index = next((i for i, s in enumerate(artifact.steps) if s.id == outcome.step_id), None)
        if index is None:
            return run.fail(
                "PRECONDITION_FAILED", step_id,
                f"restart_from names a step in this capability ({[s.id for s in artifact.steps]})",
                f"restart_from named {outcome.step_id!r}, which this artifact does not declare",
                capture=False,
            )
        return _run_from(run, artifact, index, escalator)
    # Resolved.
    if pre_act:
        return _run_from(run, artifact, step_index, escalator)  # T never acted -- re-run it
    return _resume_after(run, artifact, step_id=step_id, step_index=step_index, escalator=escalator)


def _resume_after(
    run: _Run, artifact: Artifact, *, step_id: str | None, step_index: int, escalator: Escalator,
) -> ReplayResult | None:
    """Spec §7.6/E6: re-verify the resume checkpoint, then (unless the triggering step, T, was
    the artifact's own last step) re-resolve T+1's own locator, before continuing at T+1. T
    itself is never re-run here -- its own `act()` already completed by the time this
    function is reached (`_escalate_and_continue`'s `pre_act` branch, fix round 1's Important
    #2, routes a `Resolved` for T's own pre-act failure through `_run_from(step_index)`
    instead, before this function is ever called); a human confirming `Resolved` here is
    confirming the resulting state, not asking for T's action to happen a second time.
    `step_index` is the checkpoint tail's own `len(artifact.steps)` when T was the synthetic
    checkpoint itself (`step_id is None`), in which case `step_index == 0` never triggers (an
    artifact always has at least one real step by the time replay reaches its checkpoint) but
    the branch below still holds structurally.
    """
    if step_index == 0:
        matchers: list[Matcher] = []
    else:
        matchers = _continue_matchers(artifact.steps[step_index - 1].expects)
    checkpoint = matchers[0] if matchers else artifact.success.checkpoint

    checkpoint_probe = Step(
        id="_resume_checkpoint", action="wait_for", risk="safe",
        expects=[Expect(when=checkpoint, outcome="continue", source="observed")],
    )
    verified = _settle_step(run, checkpoint_probe, None)
    if isinstance(verified, Failure) and verified.kind == "ALLOWLIST_VIOLATION":
        return verified  # D40: never escalated (review finding #7)
    if verified is not None:
        if isinstance(verified, Failure):
            return _escalate_and_continue(run, artifact, step_id=step_id, step_index=step_index,
                                          escalator=escalator, kind=verified.kind,
                                          expected=verified.expected, observed=verified.observed)
        return verified  # a BusinessOutcome ends the replay as-is -- vanishingly unlikely here

    if step_id is not None:
        run.steps_run.append(step_id)  # T's action already ran; the human confirmed it's fine

    next_index = step_index + 1
    if next_index >= len(artifact.steps):
        return _run_from(run, artifact, next_index, escalator)  # straight to the checkpoint tail

    next_step = artifact.steps[next_index]
    if next_step.locator is not None:
        try:
            resolution = run.surface.resolve(next_step.locator)
        except SurfaceError as exc:
            violated = _violation(run, next_step.id)
            if violated is not None:
                return violated
            return _escalate_and_continue(run, artifact, step_id=step_id, step_index=step_index,
                                          escalator=escalator, kind="SESSION_LOST",
                                          expected="the surface resolves the next step's locator",
                                          observed=str(exc))
        if resolution.kind != "unique":
            violated = _violation(run, next_step.id)  # review finding #7: this site was missing
            if violated is not None:                  # the D40 guard in the contract card's draft
                return violated
            kind, expected, observed = _resolution_failure_parts(resolution, next_step.locator)
            return _escalate_and_continue(run, artifact, step_id=step_id, step_index=step_index,
                                          escalator=escalator, kind=kind, expected=expected,
                                          observed=observed)

    return _run_from(run, artifact, next_index, escalator)


def _run_from(
    run: _Run, artifact: Artifact, start_index: int, escalator: Escalator | None,
) -> ReplayResult:
    """Runs `artifact.steps[start_index:]` plus the checkpoint settle, exactly what the
    original `replay()` loop did for `start_index=0` -- factored out so `Resolved` and
    `RestartFrom` can re-enter it partway through without duplicating the loop.
    """
    for index, step in enumerate(artifact.steps[start_index:], start=start_index):
        ended = _run_step(run, artifact, index, step, escalator)
        if ended is not None:
            return ended

    checkpoint_step = Step(
        id="_checkpoint", action="wait_for", risk="safe",
        expects=[Expect(when=artifact.success.checkpoint, outcome="continue", source="observed")],
    )
    ended = _settle_step(run, checkpoint_step, None)
    if isinstance(ended, Failure) and ended.kind == "ALLOWLIST_VIOLATION":
        return ended  # settle already saw it and reported it once -- D40, never escalated
                       # (review finding #8: the contract card's draft dropped this exact
                       # three-line shape `replay()`'s own current tail already uses)
    violated = _violation(run, None)
    if violated is not None:
        return violated
    if ended is not None:
        return _maybe_escalate_checkpoint(run, artifact, ended, escalator)

    missing = [name for name in artifact.outputs if name not in run.values]
    if missing:
        return run.fail(
            "OUTPUT_VALIDATION_FAILED", None,
            f"every declared output is bound by a step: {sorted(artifact.outputs)}",
            f"no step bound {missing}", capture=True,
        )
    outputs = {key: value for key, value in run.values.items() if not key.startswith("_")}
    return Success(outputs=outputs, steps_run=run.steps_run, evidence_ref=run.sink.evidence_ref())


def replay(
    artifact: Artifact, inputs: dict[str, object], surface: Surface, mode: Mode, *,
    deployment: DeploymentAllowlist | None = None, status: RegistryStatus = "draft",
    confirm_irreversible: bool = False, idempotency_key: str | None = None,
    evidence: EvidenceSink | None = None, clock: Clock | None = None,
    escalator: Escalator | None = None,
) -> ReplayResult:
    """Replays `artifact` against `surface` with `inputs`, returning exactly one
    `Success | BusinessOutcome | Failure` and never raising for a business-meaningful
    outcome (E28). `mode="supervised"` with no `escalator` raises `NotImplementedError` --
    a supervised replay with nothing to hand an escalation to cannot do anything other
    than an embedded one would, so it refuses rather than silently behaving like one (D31).

    Gate order, all before the step loop starts and all without touching `surface` (E21):
    `validate_inputs` (E22, re-stamped with this run's `evidence_ref`); `_policy_gate`
    (E17's expect-code scan, then E5/E6's per-step action/status gate, then E9/E27's
    irreversible/idempotency gate). Then `_run_from` runs the step loop (`_run_step` per
    step), the E18 checkpoint settle, and the E28 unbound-output check before `Success`.

    Phase 5: `deployment` is narrowed by `artifact.policy` (E6) before the gate ever sees
    it, so the effective allowlist is always the intersection whether or not
    `cua.artifact.validate.validate` ran over this artifact. After every `act`, every
    step's settle, and the checkpoint's settle, `_violation` asks the surface whether an
    application-initiated navigation was refused (E3) -- ahead of any other reading of
    what the surface returned or raised, and even after `Success` would otherwise follow.

    Phase 6: `mode="supervised"` with an `escalator` routes an escalation-worthy `Failure`
    (E5: a settle timeout, a recovery rule's `Escalate` outcome, or any `Failure` on an
    `irreversible` step) through `escalator.escalate(...)` instead of returning it, and
    resumes the step loop from wherever the human's `HandbackOutcome` says to (E6) --
    `_run_from` is what makes that loop re-enterable. `escalator=None` in either mode, or
    `mode="embedded"` with one supplied, behaves exactly as before this phase (E2: this
    module never imports `cua.session`; `Escalator` is a structural protocol any conforming
    object satisfies).
    """
    if mode == "supervised" and escalator is None:
        raise NotImplementedError("supervised mode is not yet implemented")

    sink: EvidenceSink = evidence if evidence is not None else _NullEvidenceSink()
    active_clock: Clock = clock if clock is not None else SYSTEM_CLOCK
    deadline = active_clock.monotonic_ms() + artifact.max_duration_ms

    input_failure = validate_inputs(artifact, inputs)
    if input_failure is not None:
        return input_failure.model_copy(update={"evidence_ref": sink.evidence_ref()})

    effective = deployment.narrowed_by(artifact.policy) if deployment is not None else None

    blocked = _policy_gate(artifact, effective, status, confirm_irreversible, idempotency_key,
                           sink)
    if blocked is not None:
        return blocked

    run = _Run(
        artifact=artifact, inputs=inputs, surface=surface, sink=sink, clock=active_clock,
        deadline=deadline, deployment=effective, idempotency_key=idempotency_key, mode=mode,
    )
    return _run_from(run, artifact, 0, escalator)
