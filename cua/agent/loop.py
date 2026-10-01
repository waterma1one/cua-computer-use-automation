"""The discovery loop (spec §8.1): the model receives the filtered observation and the
discovery tool schemas, emits one action per turn, and the loop stops on one of five
distinct, recorded reasons.

Acts only through the `Surface` protocol -- no Playwright, no DOM concept -- exactly like
`cua/replay/engine.py`. `EvidenceSink` (imported, not redeclared, from `cua.replay.engine`,
the same structural protocol replay already uses) is optional; a caller that supplies one
gets one `.event(...)` per turn, matching every other phase's evidence convention.

Time is read only through the injected `Clock`, and the loop never sleeps: a provider
failure (`LLMError`) is one failed turn, counted like a malformed one, and any retry with
backoff belongs to the caller that owns the real client, not here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Literal

from cua.agent.prompt import PromptInput, build_messages
from cua.agent.tools import DISCOVERY_ONLY_TOOL_NAMES, DISCOVERY_TOOLS, validate_tool_call
from cua.artifact.models import App, InputSpec
from cua.llm.base import Completion, LLMClient, Message, ToolCall
from cua.llm.gemini import LLMError
from cua.policy.config import PolicyConfig
from cua.policy.risk import classify
from cua.replay.engine import EvidenceSink
from cua.replay.settle import SYSTEM_CLOCK, Clock, settle_observation
from cua.surface.base import StaleObservationError, Surface, SurfaceError
from cua.surface.models import Action, ActionKind, ActionResult, Locator, Node, Observation

__all__ = [
    "DeclaredInput",
    "DiscoveryLimits",
    "StopReason",
    "Trace",
    "TraceStep",
    "discover",
]

StopReason = Literal[
    "max_steps", "max_duration", "dead_end", "consecutive_failures", "finish", "give_up",
]


@dataclass(frozen=True)
class DeclaredInput:
    """A named input this discovery run is exercising, and the literal value used for it --
    what the compiler (Task 6) matches a step's own literal against to promote it to
    `from_input` (E9).
    """

    spec: InputSpec
    example_value: str


@dataclass
class DiscoveryLimits:
    """The five stopping conditions' own thresholds, spec §8.1. Defaults are this project's
    own choice, not spec-mandated -- generous enough for a real multi-step capability, tight
    enough that a genuinely stuck loop stops within a couple of minutes.

    `dead_end_repeats` counts consecutive re-observations (after an executed action, a
    failed one, or an `expand`) whose digest equals the one before: N turns in a row that
    changed nothing on the page. Turns that never touch the surface (malformed, refused,
    provider errors) neither advance nor reset it -- they belong to the failure counter. A
    successful `read` is the same: it is expected to leave the page alone, so a flow reading
    several outputs from one static page is not mistaken for a stuck loop.

    `settle_timeout_ms` and `settle_poll_ms` bound the wait after a successful
    state-changing action (everything except `read` and `wait_for`): the loop polls until
    the page has changed and holds still for one poll, or the timeout passes. An action with
    no visible effect therefore costs the full timeout.
    """

    max_steps: int = 40
    max_duration_ms: int = 300_000
    dead_end_repeats: int = 3
    max_consecutive_failures: int = 3
    settle_timeout_ms: int = 2_000
    settle_poll_ms: int = 100


@dataclass(frozen=True)
class TraceStep:
    """One recorded, successfully-executed action. Never constructed for a discovery-only
    action or a failed one -- both are recorded to `evidence` (if given) but never appear
    here, so the compiler's own step-1 filter (spec §8.3) has nothing to do beyond reading
    `Trace.steps` as-is. `tool_call` carries the type-checked arguments actually executed
    (an integral float index is recorded as an `int`).
    """

    index: int
    tool_call: ToolCall
    discovery_only: bool
    human_origin: bool
    ok: bool
    read_value: str | None
    locator: Locator | None
    observation: Observation
    raw_nodes: list[Node]


@dataclass(frozen=True)
class Trace:
    """`final_observation` is the observation the loop held at the moment it stopped --
    for a `finish` stop, this is what the model was looking at when it named
    `checkpoint_index`. It is deliberately not `steps[-1].observation`: that field holds
    the *pre-action* observation of the last recorded step, one `surface.observe()` behind
    whenever at least one action was taken before finishing. The compiler's checkpoint must
    read this field, never the last step's own observation.
    """

    goal: str
    target: App
    declared_inputs: dict[str, DeclaredInput]
    steps: list[TraceStep]
    stop_reason: StopReason
    stop_detail: str
    checkpoint_index: int | None = None
    final_observation: Observation | None = None


@dataclass
class _NullSink:
    run_id: str = "discovery-no-evidence"

    def event(self, **fields: object) -> None:
        pass

    def frame(self, frame: object, name: str) -> None:
        pass

    def evidence_ref(self) -> str:
        return self.run_id


# Argument JSON-Schema types, read off the tool declarations themselves -- one vocabulary,
# not a second copy. `validate_tool_call` checks names and presence; this checks types.
def _declared_arg_types() -> dict[str, dict[str, str]]:
    types: dict[str, dict[str, str]] = {}
    for tool in DISCOVERY_TOOLS:
        properties = tool.parameters.get("properties", {})
        if not isinstance(properties, dict):
            raise TypeError(f"tool {tool.name!r} declares non-mapping properties")
        types[tool.name] = {name: str(schema["type"]) for name, schema in properties.items()
                            if isinstance(schema, dict)}
    return types


_ARG_TYPES: dict[str, dict[str, str]] = _declared_arg_types()

# Tools whose success leaves the page as it was, so the loop observes once without settling.
_NON_MUTATING_TOOLS: frozenset[str] = frozenset({"read", "wait_for"})

# Arguments that name a position in the current observation.
_INDEX_ARGS: frozenset[str] = frozenset({"index", "checkpoint_index"})


def _typed_call(call: ToolCall, node_count: int) -> ToolCall | str:
    """`call` with its arguments type-checked against the tool's declared schema (and an
    integral float such as `2.0` normalised to `2`), or a message saying why it is
    malformed. Booleans are never integers here, and an index must name a node in the
    current observation."""
    declared = _ARG_TYPES[call.name]
    args: dict[str, object] = {}
    for name, value in call.args.items():
        kind = declared.get(name)
        if kind == "integer":
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            if isinstance(value, bool) or not isinstance(value, int):
                return f"{call.name!r} argument {name!r} must be an integer, got {value!r}"
            if name in _INDEX_ARGS and not 0 <= value < node_count:
                return (f"{call.name!r} argument {name!r}={value} names no control; the "
                        f"current observation has indices 0..{node_count - 1}")
        elif kind == "string" and not isinstance(value, str):
            return f"{call.name!r} argument {name!r} must be a string, got {value!r}"
        args[name] = value
    return replace(call, args=args)


def _digest(observation: Observation) -> tuple[object, ...]:
    return tuple(
        (n.role, n.name, n.value, n.state.disabled, n.state.checked, n.state.expanded)
        for n in observation.nodes
    )


def _policy_refusal(action: ActionKind, name_or_path: str | None,
                    policy: PolicyConfig) -> str | None:
    """E5: `sandbox` executes everything; `strict` refuses anything `classify` does not call
    `safe`, before it reaches the surface."""
    if policy.policy_mode == "sandbox":
        return None
    risk = classify(action, name_or_path)
    if risk != "safe":
        return (f"{action!r} on {name_or_path!r} is classified {risk!r}; "
                "refused under policy_mode=strict")
    return None


# A placeholder the model types to use a declared input: the whole `value`, nothing else.
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_VALUE_TOOLS: frozenset[str] = frozenset({"fill", "select", "press_key"})


def _placeholder_name(value: str) -> str | None:
    match = _PLACEHOLDER.fullmatch(value)
    return match.group(1) if match else None


def _substitute(call: ToolCall, declared: dict[str, DeclaredInput]) -> str | None:
    """The real value to hand the surface for `call`'s `value` (the text itself when it holds
    no placeholder), or `None` if the call has no value. Raises `ValueError`, with a message
    for the model, on a placeholder that names no declared input or is not the whole value."""
    value = call.args.get("value")
    if call.name not in _VALUE_TOOLS or not isinstance(value, str):
        return value if isinstance(value, str) else None
    name = _placeholder_name(value)
    if name is not None:
        if name not in declared:
            known = ", ".join(sorted(declared)) or "none"
            raise ValueError(f"placeholder {{{{{name}}}}} is not a declared input; "
                             f"declared inputs: {known}")
        return declared[name].example_value
    if _PLACEHOLDER.search(value):
        raise ValueError("a placeholder must be the whole value, not part of longer text")
    return value


def _describe(call: ToolCall) -> str:
    return f"called {call.name}({json.dumps(call.args, sort_keys=True, default=str)})"


def discover(
    goal: str, target: App, surface: Surface, policy: PolicyConfig, llm: LLMClient, *,
    declared_inputs: dict[str, DeclaredInput] | None = None,
    limits: DiscoveryLimits | None = None,
    evidence: EvidenceSink | None = None,
    clock: Clock | None = None,
) -> Trace:
    declared = declared_inputs or {}
    prompt_inputs = [PromptInput(name=n, sensitive=d.spec.sensitive, example=d.example_value)
                     for n, d in sorted(declared.items())]
    secrets = sorted(((d.example_value, n) for n, d in declared.items()
                      if d.spec.sensitive and d.example_value), key=lambda p: -len(p[0]))

    def mask(text: str) -> str:
        """`text` with any sensitive declared value replaced by its placeholder, for every
        message the model or the evidence log is shown."""
        for secret, name in secrets:
            text = text.replace(secret, f"{{{{{name}}}}}")
        return text

    active_limits = limits or DiscoveryLimits()
    sink: EvidenceSink = evidence if evidence is not None else _NullSink()
    active_clock: Clock = clock if clock is not None else SYSTEM_CLOCK

    steps: list[TraceStep] = []
    history: list[Message] = []
    consecutive_failures = 0
    unchanged = 0
    start_ms = active_clock.monotonic_ms()
    try:
        observation = surface.observe()
    except (StaleObservationError, SurfaceError) as exc:
        # Nothing was ever seen, so there is no observation to hand back. D40: an allowlist
        # violation behind the error is named as such.
        violation = surface.allowlist_violation()
        detail = (f"allowlist violation: {violation}" if violation is not None
                  else f"initial observe failed: {exc}")
        sink.event(kind="discovery_stop", reason="consecutive_failures", detail=detail,
                   step_count=0)
        return Trace(goal=goal, target=target, declared_inputs=declared, steps=steps,
                     stop_reason="consecutive_failures", stop_detail=detail,
                     final_observation=None)

    def stop(reason: StopReason, detail: str, *, checkpoint_index: int | None = None) -> Trace:
        sink.event(kind="discovery_stop", reason=reason, detail=detail, step_count=len(steps))
        return Trace(goal=goal, target=target, declared_inputs=declared, steps=steps,
                     stop_reason=reason, stop_detail=detail, checkpoint_index=checkpoint_index,
                     final_observation=observation)

    def failed(event_kind: str, step_num: int, detail: str) -> Trace | None:
        """One malformed, refused or failed turn. Returns the stopping `Trace` once the
        consecutive-failure threshold is reached, `None` to keep going."""
        nonlocal consecutive_failures
        consecutive_failures += 1
        sink.event(kind=event_kind, step=step_num, detail=detail)
        if consecutive_failures >= active_limits.max_consecutive_failures:
            return stop("consecutive_failures", detail)
        return None

    def reobserve(fresh: Observation, *, counts: bool = True) -> None:
        """Adopt `fresh`. `counts=False` (a `read`, which never changes a page by design)
        leaves the unchanged counter exactly where it was."""
        nonlocal observation, unchanged
        if counts:
            unchanged = unchanged + 1 if _digest(fresh) == _digest(observation) else 0
        observation = fresh

    def violation_stop() -> Trace | None:
        violation = surface.allowlist_violation()
        if violation is None:
            return None
        return stop("consecutive_failures", f"allowlist violation: {violation}")

    def surface_stop(exc: Exception, doing: str) -> Trace:
        """The surface raised while the loop was only looking (observe, snapshot) -- there is
        nothing to retry against, so the run ends with a recorded reason, never a raise."""
        return violation_stop() or stop("consecutive_failures",
                                        f"surface error while {doing}: {mask(str(exc))}")

    def refresh(*, counts: bool = True, settle: bool = False) -> Trace | None:
        """Re-observe after a turn. Returns a stopping `Trace` if the surface cannot be
        observed, `None` once `observation` is current. `settle=True` (after a successful
        state-changing action) waits, through the injected clock, for the action's effect
        to land instead of adopting the first observation, which can still show the page
        the action left (a click returns before the navigation it started commits)."""
        try:
            if settle:
                fresh = settle_observation(
                    surface, _digest(observation), _digest, clock=active_clock,
                    poll_ms=active_limits.settle_poll_ms,
                    timeout_ms=active_limits.settle_timeout_ms)
            else:
                fresh = surface.observe()
        except (StaleObservationError, SurfaceError) as exc:
            return surface_stop(exc, "re-observing")
        if (halt := violation_stop()) is not None:
            return halt
        reobserve(fresh, counts=counts)
        return None

    for step_num in range(1, active_limits.max_steps + 1):
        if active_clock.monotonic_ms() - start_ms > active_limits.max_duration_ms:
            return stop("max_duration", f"exceeded {active_limits.max_duration_ms}ms")
        if unchanged >= active_limits.dead_end_repeats:
            return stop("dead_end",
                        f"observation unchanged across {unchanged} consecutive turns")

        try:
            raw = llm.step(
                build_messages(goal, observation, history, prompt_inputs), DISCOVERY_TOOLS)
        except LLMError as exc:
            if (halt := failed("llm_error", step_num, str(exc))) is not None:
                return halt
            continue

        if isinstance(raw, Completion):
            if raw.text:
                history.append(Message(role="model", text=raw.text))
            if (halt := failed("malformed_call", step_num,
                               "model returned free text instead of a tool call")) is not None:
                return halt
            continue

        problem = validate_tool_call(raw)
        typed = _typed_call(raw, len(observation.nodes)) if problem is None else problem
        history.append(Message(role="model", text=_describe(raw)))
        if isinstance(typed, str):
            history.append(Message(role="tool", text=f"error: {typed}", tool_name=raw.name,
                                   tool_call_id=raw.id))
            if (halt := failed("malformed_call", step_num, typed)) is not None:
                return halt
            continue
        call = typed

        if call.name == "finish":
            checkpoint = call.args["checkpoint_index"]
            assert isinstance(checkpoint, int)
            return stop("finish", str(call.args["summary"]), checkpoint_index=checkpoint)
        if call.name == "give_up":
            return stop("give_up", str(call.args["reason"]))
        if call.name == "expand":
            try:
                expanded = surface.expand()
            except (StaleObservationError, SurfaceError) as exc:
                if (halt := violation_stop()) is not None:
                    return halt
                history.append(Message(role="tool", text=f"error: {exc}", tool_name="expand",
                                       tool_call_id=call.id))
                if (halt := failed("failed_call", step_num, str(exc))) is not None:
                    return halt
                continue
            reobserve(expanded)
            history.append(Message(role="tool", text="expanded", tool_name="expand",
                                   tool_call_id=call.id))
            sink.event(kind="expand", step=step_num)
            consecutive_failures = 0
            continue

        kind: ActionKind = call.name  # type: ignore[assignment]  # validated above
        index = call.args.get("index")
        assert index is None or isinstance(index, int)
        path = call.args.get("path")
        subject = path if isinstance(path, str) else (
            observation.nodes[index].name if index is not None else None)
        refusal = _policy_refusal(kind, subject, policy)
        if refusal is not None:
            history.append(Message(role="tool", text=f"refused: {refusal}", tool_name=call.name,
                                   tool_call_id=call.id))
            if (halt := failed("policy_refused", step_num, refusal)) is not None:
                return halt
            continue

        try:
            real_value = _substitute(call, declared)
        except ValueError as exc:
            history.append(Message(role="tool", text=f"error: {exc}", tool_name=call.name,
                                   tool_call_id=call.id))
            if (halt := failed("malformed_call", step_num, str(exc))) is not None:
                return halt
            continue

        acted_on = observation
        try:
            action_result: ActionResult
            if index is None:
                action_result = surface.act(Action(
                    kind=kind, value=path if isinstance(path, str) else None))
            else:
                action_result = surface.act_on_index(
                    observation.generation, index, kind, real_value)
        except (StaleObservationError, SurfaceError) as exc:
            if (halt := violation_stop()) is not None:
                return halt
            history.append(Message(role="tool", text=f"error: {mask(str(exc))}",
                                   tool_name=call.name, tool_call_id=call.id))
            if (halt := refresh()) is not None:
                return halt
            if (halt := failed("failed_call", step_num, mask(str(exc)))) is not None:
                return halt
            continue

        if (halt := violation_stop()) is not None:
            return halt

        history.append(Message(
            role="tool", tool_name=call.name, tool_call_id=call.id,
            text=mask(action_result.read_value) if action_result.read_value
            else ("ok" if action_result.ok else "failed"),
        ))

        if not action_result.ok:
            if (halt := refresh()) is not None:
                return halt
            if (halt := failed("failed_call", step_num,
                               f"{call.name} reported ok=False")) is not None:
                return halt
            continue

        try:
            raw_nodes = surface.raw_snapshot()
        except (StaleObservationError, SurfaceError) as exc:
            return surface_stop(exc, "snapshotting")
        consecutive_failures = 0
        steps.append(TraceStep(
            index=step_num, tool_call=call,
            discovery_only=call.name in DISCOVERY_ONLY_TOOL_NAMES, human_origin=False,
            ok=True, read_value=action_result.read_value, locator=action_result.action.locator,
            observation=acted_on, raw_nodes=raw_nodes,
        ))
        sink.event(kind="action", step=step_num, tool=call.name, ok=True)
        if (halt := refresh(counts=call.name != "read",
                            settle=call.name not in _NON_MUTATING_TOOLS)) is not None:
            return halt

    return stop("max_steps", f"reached {active_limits.max_steps}")
