"""The deterministic replay step loop: turns an `Artifact` plus caller-supplied `inputs`
into exactly one `Success | BusinessOutcome | Failure` (`cua.replay.result.ReplayResult`),
acting on the target application only through the `Surface` protocol (D19).

No Playwright, no Selenium, no DOM concept, no CSS selector or XPath anywhere in this
module -- `tests/test_architecture.py` greps `cua/replay/` for exactly that, and this
module additionally never imports `cua.observability` (phase 5's package, which does not
exist yet): `EvidenceSink` below is a local `Protocol` so this module can be written,
tested and reviewed independently of how a run's evidence is actually persisted.

Every wait comes from the injected `Clock` (default `cua.replay.settle.SYSTEM_CLOCK`),
never a fixed `sleep` -- `settle()` already enforces this for the per-step poll loop, and
this module never sleeps on its own.

Control-flow rulings this module implements, restated briefly (the task brief carries the
full reasoning): E9 (irreversible/idempotency gate), E10/E22 (`validate_inputs` as its own
public function), E14 (an `ok=False` action result with a pending dialog falls through to
`settle()`; without one it is `PRECONDITION_FAILED`), E17 (a `fail`/`business` expect with
a malformed code is refused before the surface is ever touched), E18 (the artifact's
`success.checkpoint` is itself settled, as a synthetic step, after the last real step),
E21 (a frame is captured only for a failure that arises during or after a surface
interaction -- every pre-loop gate and every pre-act refusal passes `capture=False` and
never calls a `Surface` method at all).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, get_args, runtime_checkable

from cua.artifact.models import (
    Artifact,
    Expect,
    FailureKind,
    FromInput,
    LiteralValue,
    Matcher,
    Step,
    StepValue,
)
from cua.artifact.validate import DeploymentAllowlist
from cua.replay.extract import ParseError, extract
from cua.replay.result import BusinessOutcome, Failure, Mode, ReplayResult, Success, mint_run_id
from cua.replay.settle import (
    SYSTEM_CLOCK,
    BranchOutcome,
    Business,
    Clock,
    Continue,
    DialogUnhandled,
    Escalate,
    Fail,
    settle,
)
from cua.surface.base import Surface, SurfaceError
from cua.surface.models import Action, EvidenceFrame, Locator, Node, Observation

__all__ = ["EvidenceSink", "replay", "validate_inputs"]

# E4'/E17: the closed set a `fail` expect's `code` must name. `load()` (via
# `cua.artifact.validate`) already refuses to persist an artifact carrying a bad one, but
# this module must not trust that every artifact it is handed went through `load()` --
# a hand-built or hand-edited artifact reaching `replay()` directly is exactly the case
# this pre-loop scan exists for.
_FAILURE_KINDS: frozenset[str] = frozenset(get_args(FailureKind))

# E9: an irreversible step, once actually attempted under a given idempotency key, must
# not be attempted again under the same key -- in-memory, for the lifetime of this
# process. Phase 9's console or a durable store is a later phase's job; this is the
# minimal control that keeps a duplicate "post" from ever reaching the surface twice.
_used_idempotency_keys: set[str] = set()


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
                    observed=f"input {name!r} was {value!r} ({type(value).__name__})",
                    evidence_ref=sink.evidence_ref(),
                )
            if spec.pattern is not None and re.fullmatch(spec.pattern, value) is None:
                return Failure(
                    kind="INVALID_INPUT", step_id=None,
                    expected=f"input {name!r} matches pattern {spec.pattern!r}",
                    observed=f"input {name!r} was {value!r}",
                    evidence_ref=sink.evidence_ref(),
                )
        elif spec.type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                return Failure(
                    kind="INVALID_INPUT", step_id=None,
                    expected=f"input {name!r} is an integer",
                    observed=f"input {name!r} was {value!r} ({type(value).__name__})",
                    evidence_ref=sink.evidence_ref(),
                )
        # Any other declared `type` is not validated here -- the stated limit above.
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
    explanation. Otherwise `surface.capture()` is attempted, swallowing `SurfaceError`
    (a failed capture must never hide the `Failure` it was trying to illustrate). When
    `capture` is `False`, `surface` is never touched at all -- what every
    `PoisonSurface`-based test in this task's suite relies on.
    """
    if capture:
        message = surface.pending_dialog()
        if message is not None:
            sink.event(step_id=step_id, kind="capture_skipped",
                       reason="a native dialog is pending")
        else:
            try:
                frame = surface.capture()
            except SurfaceError:
                pass
            else:
                sink.frame(frame, step_id or "input")
    return Failure(kind=kind, step_id=step_id, expected=expected, observed=observed,
                    evidence_ref=sink.evidence_ref())


def _describe_matcher(matcher: Matcher) -> str:
    role = f"role={matcher.role!r} " if matcher.role else ""
    return f"{matcher.strategy} {role}name={matcher.name!r} ({matcher.name_match})"


def _describe_expects(expects: list[Expect]) -> str:
    if not expects:
        return "the step to settle with nothing further declared"
    return "one of: " + "; ".join(f"{e.outcome} on {_describe_matcher(e.when)}" for e in expects)


def _describe_observation(observation: Observation) -> str:
    if not observation.nodes:
        return "no nodes were observed before the timeout"
    parts = [n.name or n.value or f"an unnamed {n.role}" for n in observation.nodes]
    return "observed: " + "; ".join(parts)


def _describe_locator(locator: Locator) -> str:
    role = f"role={locator.role!r} " if locator.role else ""
    return f"a unique control matching {locator.strategy} {role}name={locator.name!r}"


def _permits_path(path: str, prefixes: list[str]) -> bool:
    return any(path.startswith(prefix) for prefix in prefixes)


def _denied_navigation_reason(path: str, deployment: DeploymentAllowlist) -> str | None:
    """E8: the static, declared-target half of the navigation allowlist -- mirrors
    `cua.artifact.validate._narrowing_findings`'s deny-first, prefix-matched logic. Live
    enforcement (redirects, a page that navigates itself) is phase 5's; this only catches
    an artifact whose own declared `target.path` the deployment does not permit.
    """
    if _permits_path(path, deployment.denied_paths):
        return f"path {path!r} matches a denied prefix on the deployment allowlist"
    if not _permits_path(path, deployment.allowed_paths):
        return f"path {path!r} does not match any allowed prefix on the deployment allowlist"
    return None


def _resolve_step_value(
    value: StepValue | None, inputs: dict[str, object], values: dict[str, object],
) -> str | None:
    """Resolves one step's `value` (a `FromInput`/`LiteralValue`/`FromStep`) to the plain
    string an `Action` carries. `values` holds every prior step's `into` binding,
    including underscore-prefixed locals.
    """
    if value is None:
        return None
    if isinstance(value, FromInput):
        return str(inputs[value.from_input])
    if isinstance(value, LiteralValue):
        return value.literal
    return str(values[value.from_step])  # FromStep: the only StepValue arm left


def _translate_outcome(
    outcome: BranchOutcome, step_id: str | None, expected_description: str, *,
    surface: Surface, sink: EvidenceSink,
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
        assert step_id is not None, "a business outcome only arises from a step's own expects"
        assert outcome.code is not None, "E17's pre-loop gate guarantees a business code"
        return BusinessOutcome(code=outcome.code, step_id=step_id, message=outcome.message,
                                evidence_ref=sink.evidence_ref())
    if isinstance(outcome, Fail):
        return _fail(
            outcome.code, step_id, expected_description,
            outcome.matched_text or "the matched fail condition carried no observable text",
            surface=surface, sink=sink, capture=True,
        )
    if isinstance(outcome, Escalate):
        return _fail(
            "ESCALATION_UNAVAILABLE", step_id,
            "the triggering condition resolves without a human",
            (
                f"recovery rule {outcome.recovery_name!r} requires escalation, which an "
                "embedded replay cannot provide"
            ),
            surface=surface, sink=sink, capture=True,
        )
    if isinstance(outcome, DialogUnhandled):
        return _fail(
            "UNHANDLED_DIALOG", step_id, "a recovery rule handles the pending dialog",
            f"an unhandled dialog appeared: {outcome.message!r}",
            surface=surface, sink=sink, capture=True,
        )
    # The only BranchOutcome variant left is TimedOut.
    return _fail(
        "NO_BRANCH_MATCHED", step_id, expected_description,
        _describe_observation(outcome.observation),
        surface=surface, sink=sink, capture=True,
    )


def replay(
    artifact: Artifact, inputs: dict[str, object], surface: Surface, mode: Mode, *,
    deployment: DeploymentAllowlist | None = None, confirm_irreversible: bool = False,
    idempotency_key: str | None = None, evidence: EvidenceSink | None = None,
    clock: Clock | None = None,
) -> ReplayResult:
    """Replays `artifact` against `surface` with `inputs`, returning exactly one
    `Success | BusinessOutcome | Failure` and never raising for a business-meaningful
    outcome. `mode="supervised"` raises `NotImplementedError` -- this phase implements
    unattended (`"embedded"`) replay only.

    Gate order, all before the step loop starts and all `capture=False` (E21) because none
    of them has touched `surface` yet: `validate_inputs` (E22); a scan of every step's
    `expects` for a `fail` clause naming an unknown code or a `business` clause naming none
    (E17); the irreversible/idempotency gate (E9).
    """
    if mode == "supervised":
        raise NotImplementedError("supervised mode is not yet implemented")

    sink: EvidenceSink = evidence if evidence is not None else _NullEvidenceSink()
    active_clock: Clock = clock if clock is not None else SYSTEM_CLOCK
    deadline = active_clock.monotonic_ms() + artifact.max_duration_ms

    input_failure = validate_inputs(artifact, inputs)
    if input_failure is not None:
        return input_failure

    for scanned in artifact.steps:
        for expect in scanned.expects:
            if expect.outcome == "fail" and expect.code not in _FAILURE_KINDS:
                return Failure(
                    kind="POLICY_BLOCKED", step_id=scanned.id,
                    expected="a fail expect names a valid FailureKind",
                    observed=(
                        f"step {scanned.id!r} has a fail expect with code {expect.code!r}"
                    ),
                    evidence_ref=sink.evidence_ref(),
                )
            if expect.outcome == "business" and not expect.code:
                return Failure(
                    kind="POLICY_BLOCKED", step_id=scanned.id,
                    expected="a business expect names a code",
                    observed=f"step {scanned.id!r} has a business expect with no code",
                    evidence_ref=sink.evidence_ref(),
                )

    first_irreversible = next(
        (s for s in artifact.steps if s.risk == "irreversible"), None,
    )
    if first_irreversible is not None:
        if not confirm_irreversible:
            return Failure(
                kind="POLICY_BLOCKED", step_id=first_irreversible.id,
                expected="an irreversible step requires confirm_irreversible=True",
                observed=(
                    f"step {first_irreversible.id!r} is irreversible and "
                    "confirm_irreversible was not set"
                ),
                evidence_ref=sink.evidence_ref(),
            )
        if idempotency_key is not None:
            if idempotency_key in _used_idempotency_keys:
                return Failure(
                    kind="POLICY_BLOCKED", step_id=first_irreversible.id,
                    expected="idempotency_key names an operation not already performed",
                    observed=f"idempotency_key {idempotency_key!r} has already been used",
                    evidence_ref=sink.evidence_ref(),
                )
            _used_idempotency_keys.add(idempotency_key)

    values: dict[str, object] = {}
    steps_run: list[str] = []

    for step in artifact.steps:
        if step.risk is None:
            return _fail(
                "POLICY_BLOCKED", step.id,
                "every replayed step carries a risk classification",
                f"step {step.id!r} has risk=None",
                surface=surface, sink=sink, capture=False,
            )

        if active_clock.monotonic_ms() >= deadline:
            return _fail(
                "DURATION_EXCEEDED", step.id,
                f"the replay completes within {artifact.max_duration_ms}ms",
                (
                    f"the {artifact.max_duration_ms}ms deadline was reached before step "
                    f"{step.id!r} began"
                ),
                surface=surface, sink=sink, capture=bool(steps_run),
            )

        resolved_node: Node | None = None

        if step.action == "navigate":
            path = step.target.path if step.target is not None else ""
            if deployment is not None:
                reason = _denied_navigation_reason(path, deployment)
                if reason is not None:
                    return _fail(
                        "ALLOWLIST_VIOLATION", step.id,
                        "the navigate target is permitted by the deployment allowlist",
                        reason, surface=surface, sink=sink, capture=False,
                    )
            action = Action(kind="navigate", locator=None, value=path)
        else:
            if step.locator is not None:
                resolution = surface.resolve(step.locator)
                if resolution.kind == "not_found":
                    return _fail(
                        "LOCATOR_NOT_FOUND", step.id, _describe_locator(step.locator),
                        resolution.reason, surface=surface, sink=sink, capture=True,
                    )
                if resolution.kind == "ambiguous":
                    return _fail(
                        "AMBIGUOUS_LOCATOR", step.id,
                        _describe_locator(step.locator) + " to resolve to exactly one control",
                        f"{resolution.count} controls matched",
                        surface=surface, sink=sink, capture=True,
                    )
                if resolution.kind == "precondition_failed":
                    return _fail(
                        "PRECONDITION_FAILED", step.id, f"the control is {resolution.which}",
                        f"the control failed the {resolution.which} precondition",
                        surface=surface, sink=sink, capture=True,
                    )
                resolved_node = resolution.node
            value = _resolve_step_value(step.value, inputs, values)
            action = Action(kind=step.action, locator=step.locator, value=value)

        try:
            result = surface.act(action)
        except SurfaceError as exc:
            return _fail(
                "SESSION_LOST", step.id, "the surface completes the action without error",
                str(exc), surface=surface, sink=sink, capture=True,
            )

        if not result.ok:
            dialog_message = surface.pending_dialog()
            if dialog_message is None:
                return _fail(
                    "PRECONDITION_FAILED", step.id, "the action completes successfully",
                    result.read_value or f"the {step.action} action did not complete",
                    surface=surface, sink=sink, capture=True,
                )
            # E14: a pending dialog reaches the recovery machinery through settle() below,
            # instead of being reported here as a precondition failure.

        wants_extraction = (
            step.into is not None or step.extract is not None or step.parse is not None
        )
        if wants_extraction and resolved_node is not None:
            try:
                extracted = extract(resolved_node, step.extract or "text", step.parse)
            except ParseError as exc:
                return _fail(
                    "OUTPUT_VALIDATION_FAILED", step.id,
                    f"a value parseable as {step.parse or 'text'}",
                    str(exc), surface=surface, sink=sink, capture=True,
                )
            if step.into is not None:
                values[step.into] = extracted

        outcome = settle(surface, step, artifact.settle, artifact.recovery, clock=active_clock)
        branch_result = _translate_outcome(
            outcome, step.id, _describe_expects(step.expects), surface=surface, sink=sink,
        )
        if branch_result is not None:
            return branch_result

        steps_run.append(step.id)

    # E18: the artifact's own success checkpoint is settled as a synthetic single-clause
    # step, after the last real step -- not assumed from the last step's own `continue`.
    checkpoint_expect = Expect(when=artifact.success.checkpoint, outcome="continue",
                                source="observed")
    checkpoint_step = Step(id="_checkpoint", action="wait_for", expects=[checkpoint_expect],
                            risk="safe")
    outcome = settle(surface, checkpoint_step, artifact.settle, artifact.recovery,
                      clock=active_clock)
    branch_result = _translate_outcome(
        outcome, None, _describe_expects(checkpoint_step.expects), surface=surface, sink=sink,
    )
    if branch_result is not None:
        return branch_result

    outputs = {key: value for key, value in values.items() if not key.startswith("_")}
    return Success(outputs=outputs, steps_run=steps_run, evidence_ref=sink.evidence_ref())
