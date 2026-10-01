"""Spec §8.3, steps 1-6: mechanical, offline compilation of a Trace into a candidate
Artifact. Step 7 (self-verification, live) is `cua.artifact.compile.self_verify`, Task 7 --
kept in this same module (compilation's whole point) but added in its own task because it is
the one part of this file that needs a live browser to test.

The model contributes metadata only (parameter names via `output_name`, the checkpoint via
`finish`'s own index) -- every Locator here was already synthesized by `act_on_index` at
discovery time (phase 2's `synthesize`, reused, never recomputed) and is re-verified against
the same step's own captured, unfiltered snapshot (E3's `raw_snapshot`), not against a live
page.
"""

from __future__ import annotations

import re
from typing import Literal, cast, get_args

from cua.agent.loop import DeclaredInput, Trace, TraceStep
from cua.artifact.models import (
    App,
    Artifact,
    Expect,
    FromInput,
    FromStep,
    LiteralValue,
    Matcher,
    OutputSpec,
    Provenance,
    Settle,
    Step,
    StepValue,
    Success,
    Target,
)
from cua.artifact.validate import origin_of, validate
from cua.policy.allowlist import navigation_guard
from cua.policy.config import PolicyConfig
from cua.policy.risk import classify
from cua.replay.engine import EvidenceSink
from cua.replay.engine import replay as run_replay
from cua.replay.result import Failure
from cua.replay.result import Success as ReplaySuccess
from cua.surface.base import SurfaceError
from cua.surface.locators import resolve_against
from cua.surface.models import Action, ActionKind, Node, Observation, Unique, is_protected_name
from cua.surface.web import WebSurface, launch_page

__all__ = ["CompileError", "compile", "self_verify"]


class CompileError(ValueError):
    """A trace cannot be mechanically compiled: an unfinished run (E10), a literal from a
    protected field with no declared-input home (E9), a locator that fails to re-verify
    against its own step's raw snapshot, or a load-time validation error (step 6)."""


def _identifier(raw: str) -> str:
    """Normalise a model-proposed parameter name to `^[a-z][a-z0-9_]*$` (D28)."""
    ident = re.sub(r"[^a-z0-9_]+", "_", raw.strip().lower()).strip("_")
    if not ident or not ident[0].isalpha():
        ident = f"p_{ident}" if ident else "value"
    return ident


def _index_arg(step: TraceStep) -> int:
    # The loop already type-checked tool-call arguments (Task 3 note); this narrows for mypy.
    return int(cast(int, step.tool_call.args.get("index", 0)))


def _matcher_from_node(node: Node) -> Matcher:
    if node.name:
        return Matcher(strategy="role_name", role=node.role, name=node.name, name_match="exact")
    return Matcher(strategy="text", role=None, name=node.value, name_match="exact")


def _promote_or_refuse(
    value: str, node: Node, declared_inputs: dict[str, DeclaredInput], local_names: dict[str, str],
) -> StepValue:
    for name, declared in declared_inputs.items():
        if value == declared.example_value:
            return FromInput(from_input=_identifier(name))
    for step_id, produced_value in local_names.items():
        if value == produced_value:
            return FromStep(from_step=step_id)
    if is_protected_name(node.name):
        raise CompileError(
            f"a literal value was typed into {node.name!r}, a protected-named field, and "
            "matches no declared input; refusing to bake a credential into the artifact"
        )
    return LiteralValue(literal=value)


def _verify_locator(step: TraceStep) -> None:
    if step.locator is None:
        return
    resolution = resolve_against(step.locator, step.raw_nodes)
    if not isinstance(resolution, Unique):
        raise CompileError(
            f"step {step.index}'s locator does not resolve uniquely against its own "
            f"captured snapshot ({type(resolution).__name__}); refusing to compile it"
        )


def _compile_step(
    step: TraceStep, next_step: TraceStep | None, declared_inputs: dict[str, DeclaredInput],
    local_bindings: dict[str, str], step_id: str, output_names: set[str],
    declared_outputs: frozenset[str],
) -> Step:
    _verify_locator(step)
    name = cast(ActionKind, step.tool_call.name)

    expects: list[Expect] = []
    if next_step is not None:
        next_node = next_step.observation.nodes[
            _index_arg(next_step)
        ] if "index" in next_step.tool_call.args else None
        if next_node is not None:
            expects = [Expect(when=_matcher_from_node(next_node), outcome="continue",
                              source="observed", verified=True)]

    if name == "navigate":
        return Step(id=step_id, action="navigate",
                    target=Target(path=str(step.tool_call.args["path"])),
                    risk=classify("navigate", str(step.tool_call.args.get("path", ""))),
                    expects=expects)

    node = step.observation.nodes[_index_arg(step)]
    value: StepValue | None = None
    if "value" in step.tool_call.args:
        value = _promote_or_refuse(
            str(step.tool_call.args["value"]), node, declared_inputs, local_bindings,
        )
    into: str | None = None
    extract: Literal["text", "value", "attribute"] | None = None
    parse: Literal["raw", "money", "int", "date"] | None = None
    if name == "read":
        raw_name = step.tool_call.args.get("output_name")
        # Only a name the caller declared as an output is bound by name; any other read is
        # a step-local value (`_sN`), which the validator accepts without a declaration.
        candidate = _identifier(str(raw_name)) if raw_name else None
        output_name = candidate if candidate in declared_outputs else None
        extract, parse = "text", "raw"
        into = output_name if output_name else step_id.replace("s", "_s", 1)
        if output_name:
            output_names.add(output_name)
        elif step.read_value is not None:
            local_bindings[step_id] = step.read_value

    return Step(id=step_id, action=name, locator=step.locator, value=value,
               risk=classify(name, node.name), expects=expects, extract=extract, parse=parse,
               into=into)


def _compile_checkpoint(final_observation: Observation, checkpoint_index: int) -> Matcher:
    nodes = final_observation.nodes
    if not (0 <= checkpoint_index < len(nodes)):
        raise CompileError(
            f"finish's checkpoint_index {checkpoint_index} is out of range for the final "
            f"observation ({len(nodes)} nodes)"
        )
    return _matcher_from_node(nodes[checkpoint_index])


def compile(
    trace: Trace, *, id: str, version: int, name: str, description: str, app: App,
    settle: Settle, max_duration_ms: int, outputs: dict[str, OutputSpec],
    provenance: Provenance,
) -> Artifact:
    if trace.stop_reason != "finish":
        raise CompileError(
            f"trace did not finish (stop_reason={trace.stop_reason!r}); nothing to compile"
        )
    recorded = [s for s in trace.steps if not s.discovery_only and not s.human_origin and s.ok]
    if not recorded:
        raise CompileError("no replay-vocabulary actions were recorded")

    local_bindings: dict[str, str] = {}
    output_names: set[str] = set()
    steps: list[Step] = []
    for i, step in enumerate(recorded):
        step_id = f"s{i + 1}"
        next_step = recorded[i + 1] if i + 1 < len(recorded) else None
        steps.append(_compile_step(
            step, next_step, trace.declared_inputs, local_bindings, step_id, output_names,
            frozenset(outputs),
        ))

    if trace.checkpoint_index is None:
        raise CompileError("a finished trace must carry finish's checkpoint_index")
    if trace.final_observation is None:
        raise CompileError("a finished trace must carry its final observation")
    checkpoint = _compile_checkpoint(trace.final_observation, trace.checkpoint_index)

    inputs = {_identifier(k): d.spec for k, d in trace.declared_inputs.items()}
    missing_outputs = set(outputs) - output_names
    if missing_outputs:
        raise CompileError(f"declared output(s) never bound by a read step: {missing_outputs}")

    artifact = Artifact(
        schema_version=1, id=id, version=version, name=name, description=description,
        verified=False, app=app, settle=settle, max_duration_ms=max_duration_ms,
        inputs=inputs, outputs=outputs, steps=steps, success=Success(checkpoint=checkpoint),
        provenance=provenance,
    )
    errors = [f for f in validate(artifact) if f.level == "error"]
    if errors:
        raise CompileError("; ".join(f"{f.code}: {f.message}" for f in errors))
    return artifact


def _same_origin_policy(base_url: str) -> PolicyConfig:
    """The allowlist self-verification uses when the caller supplies none: the one origin
    being verified against, every path, every action. Never the engine's permissive
    default (D43) -- navigation off that origin is still refused by the guard."""
    origin = origin_of(base_url)
    if origin is None:
        raise CompileError(f"self-verification needs an http(s) base URL, got {base_url!r}")
    return PolicyConfig(
        allowed_origins=[origin], allowed_paths=["/"],
        allowed_actions=list(get_args(ActionKind)),
    )


def self_verify(
    artifact: Artifact, base_url: str, inputs: dict[str, object], *,
    policy: PolicyConfig | None = None, evidence: EvidenceSink | None = None,
) -> Artifact:
    """Spec §8.3 step 7 (E11): one real replay through a brand-new `WebSurface` -- a fresh
    session, never the page discovery was left on. The only code that may return an
    artifact with `verified=True`, and only after `Success`. Any other outcome raises
    `CompileError`; the input artifact is never mutated and never saved here.

    The replay is policy-enforced exactly as `cua replay` does it: the effective allowlist
    (`policy`, narrowed by the artifact's own `policy`) gates actions and, through the
    surface's navigation guard, every application-initiated navigation. A candidate has no
    registry entry, so it replays at status `draft`: a risky or irreversible step is
    refused rather than self-approved.
    """
    deployment = policy if policy is not None else _same_origin_policy(base_url)
    errors = [f for f in validate(artifact, deployment) if f.level == "error"]
    if errors:
        raise CompileError(
            "self-verification refused an invalid artifact: "
            + "; ".join(f"{f.code}: {f.message}" for f in errors)
        )
    effective = deployment.narrowed_by(artifact.policy)
    entry = artifact.app.entry
    if not effective.permits_path(entry):
        raise CompileError(
            f"self-verification refused: entry path {entry!r} is not permitted by the policy"
        )
    with launch_page(base_url) as page:
        surface = WebSurface(page, navigation_guard=navigation_guard(effective))
        # Discovery starts on `app.entry`; the engine replays steps against whatever page
        # the surface is on, so a fresh session is put where discovery began.
        try:
            opened = surface.act(Action(kind="navigate", value=entry))
        except SurfaceError as exc:
            raise CompileError(f"self-verification could not open entry {entry!r}: {exc}") from exc
        if not opened.ok:
            raise CompileError(f"self-verification could not open entry {entry!r}")
        result = run_replay(
            artifact, inputs, surface, "embedded", deployment=effective,
            status="draft", evidence=evidence,
        )
    if isinstance(result, ReplaySuccess):
        return artifact.model_copy(update={"verified": True})
    if isinstance(result, Failure):
        raise CompileError(
            f"self-verification failed at step {result.step_id!r}: expected "
            f"{result.expected!r}, observed {result.observed!r} (kind={result.kind})"
        )
    raise CompileError(
        f"self-verification ended in a business outcome ({result.code!r}); a capability "
        "whose own discovered path does not replay cleanly is not ready to be verified"
    )
