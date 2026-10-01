"""The `cua` command-line entry point.

Only `cua discover` (a later phase) requires a model API key; this module's `replay`
command runs with no key and no network beyond the target application (RULES.md S7).

This module never names the driver package directly. Chromium is launched behind
`cua.surface.web.launch_page`, the one factory in the system permitted to do that, and
this module imports that name (rather than calling it fully qualified at the call site)
so a test can substitute it and have the replacement actually take effect.

Phase 5: `replay` now requires `--policy`, a deployment allowlist YAML (spec S6.1). It is
loaded before the artifact -- a replay never runs without one -- and, once the artifact is
loaded, `cua.artifact.validate.validate` checks that the artifact's own policy block (if
any) only narrows it, never widens it. The effective (narrowed) allowlist is what is
handed to the replay engine and to the live navigation guard installed on the `WebSurface`;
the artifact's registry status (draft/approved) is read and passed through too.

Phase 6: `serve` runs the session service (`cua.session.service.create_app`) under uvicorn,
the same shape `mockapp/__main__.py` uses for the mock app.
"""

from __future__ import annotations

import contextlib
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn, cast, get_args

import typer
import uvicorn
from dotenv import load_dotenv

from cua.agent.loop import DeclaredInput
from cua.agent.loop import discover as run_discover
from cua.artifact.compile import CompileError, self_verify
from cua.artifact.compile import compile as compile_artifact
from cua.artifact.models import App, Artifact, InputSpec, Provenance, Settle
from cua.artifact.store import RegistryEntry, load, read_registry, save, write_registry_entry
from cua.artifact.validate import validate
from cua.llm.gemini import LLMError, load_gemini_client_from_env
from cua.llm.retry import RetryingClient
from cua.observability.evidence import EvidenceWriter
from cua.policy.allowlist import navigation_guard
from cua.policy.config import load_policy
from cua.replay.engine import replay as run_replay
from cua.replay.engine import validate_inputs
from cua.replay.result import Failure, Mode, mint_run_id
from cua.session.service import create_app as create_session_app
from cua.surface.base import SurfaceError
from cua.surface.models import Action
from cua.surface.web import WebSurface, launch_page

load_dotenv()

app = typer.Typer()


@app.callback()
def _root() -> None:
    """No-op callback.

    Without one, a `typer.Typer()` carrying exactly one registered command is collapsed
    by Typer into a single-command app, and `cua replay ...` would be parsed with
    `"replay"` consumed as that one command's first positional argument rather than as
    the subcommand name (verified against typer 0.27.2). Registering this callback keeps
    `replay` addressed as a subcommand, which is what every later phase's commands will
    also attach to.
    """


_REDACTION_MARKER = "[REDACTED]"


def _refuse(writer: EvidenceWriter, failure: Failure) -> NoReturn:
    """The shared shape for every boundary problem caught after the artifact loaded but
    before `launch_page` is called (E22, E33): a malformed `--input` pair, an undeclared
    input, an input that fails to coerce to its declared type, and an input that fails
    `validate_inputs`'s own checks all end here, so there is one printed shape and one
    exit code for "the input was bad" rather than four.

    The `Failure` is re-stamped with this run's `evidence_ref` (the engine does the same
    to a `validate_inputs` refusal, `cua.replay.engine.replay`), written as
    `result.json` so the printed pointer names a directory that exists, printed as JSON,
    and the process exits 1. No browser was opened, so no frame is captured (E21).
    """
    stamped = failure.model_copy(update={"evidence_ref": writer.evidence_ref()})
    writer.write_result(stamped)
    typer.echo(stamped.model_dump_json())
    raise typer.Exit(code=1)


def _parse_pairs(pairs: list[str], input_specs: dict[str, InputSpec]) -> dict[str, str] | Failure:
    """Parses repeated `KEY=VALUE` pairs into a `dict[str, str]` of declared inputs.

    Returns an `INVALID_INPUT` `Failure` (never raises) for the two malformations that
    only exist at the command-line-string layer: a `--input` with no `KEY=VALUE`
    separator (or an empty key, e.g. `--input =5`), and a key the artifact does not
    declare. The second is what lets `EvidenceWriter.write_run` insist on an `InputSpec`
    for every input it masks (E31): an undeclared input has no `sensitive` flag to
    consult, so it is refused here rather than guessed at there.

    E31 also shapes the messages: a pair with an empty key is reported without its value
    (there is no name to look a `sensitive` flag up under), and a pair with no separator
    is echoed whole because it is a name, not a value.
    """
    raw: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if sep == "":
            return Failure(
                kind="INVALID_INPUT", step_id=None,
                expected="every --input is KEY=VALUE",
                observed=f"--input {pair!r} has no KEY=VALUE separator",
                evidence_ref=f"evidence/{mint_run_id()}",
            )
        if key == "":
            return Failure(
                kind="INVALID_INPUT", step_id=None,
                expected="every --input is KEY=VALUE",
                observed="--input has an empty KEY before its '='",
                evidence_ref=f"evidence/{mint_run_id()}",
            )
        if key not in input_specs:
            return Failure(
                kind="INVALID_INPUT", step_id=None,
                expected=f"every --input names a declared input ({sorted(input_specs)})",
                observed=f"input {key!r} is not declared by this artifact",
                evidence_ref=f"evidence/{mint_run_id()}",
            )
        raw[key] = value
    return raw


def _coerce_inputs(
    raw: dict[str, str], input_specs: dict[str, InputSpec]
) -> dict[str, object] | Failure:
    """Coerces any value whose declared `InputSpec.type == "integer"` to `int` (E20).

    Every value arriving from the command line is a string; `validate_inputs` checks
    Python types directly, so an artifact declaring an integer input would otherwise be
    refused a value it should accept. A declared `type: integer` value that does not
    parse as one (e.g. `--input count=abc`) is an `INVALID_INPUT` `Failure`, never a
    traceback. E31: when the input is declared `sensitive`, `[REDACTED]` stands in for
    the value in `observed` -- the same rule `validate_inputs` applies at its own
    construction site.
    """
    coerced: dict[str, object] = {}
    for key, value in raw.items():
        spec = input_specs[key]
        if spec.type == "integer":
            try:
                coerced[key] = int(value)
            except ValueError:
                shown = _REDACTION_MARKER if spec.sensitive else repr(value)
                return Failure(
                    kind="INVALID_INPUT", step_id=None,
                    expected=f"input {key!r} is an integer",
                    observed=f"input {key!r} was {shown}",
                    evidence_ref=f"evidence/{mint_run_id()}",
                )
        else:
            coerced[key] = value
    return coerced


@app.command()
def replay(
    artifact_id: str,
    version: int,
    root: Path = typer.Option(..., "--root", help="Artifact store root."),  # noqa: B008
    base_url: str = typer.Option(..., "--base-url", help="Base URL of the target app."),
    policy_path: Path = typer.Option(  # noqa: B008
        ..., "--policy", help="Deployment policy YAML (see policy.example.yaml)."
    ),
    evidence_root: Path = typer.Option(  # noqa: B008
        Path("."), "--evidence-root", help="Where evidence/<run_id>/ is written."
    ),
    input_pairs: list[str] = typer.Option(  # noqa: B008
        [], "--input", help="KEY=VALUE, repeatable."
    ),
    mode: str = typer.Option("embedded", "--mode", help="embedded or supervised."),
) -> None:
    """Replays one capability artifact against a live target application.

    `--policy` is required: a deployment allowlist YAML that is loaded before the
    artifact and, once the artifact loads, is checked against the artifact's own policy
    block so a capability policy can only narrow the deployment's allowlist, never widen
    it (exit 2 on either a policy that fails to load or a widening finding). The
    resulting, narrowed allowlist -- not the raw policy file -- is what the replay engine
    enforces and what the live navigation guard on the browser page checks.

    Prints the resulting `ReplayResult` as JSON to stdout -- this is the caller's channel
    and carries every output unmasked (E8) -- and exits non-zero on a `Failure`, on a
    `--mode` that is not one of `Mode`'s literals, or when `--mode supervised` is
    requested (unattended replay only, this phase). `result.json` under the printed
    `evidence_ref` is the evidence copy of the same result, with any output the artifact
    declares `redact: true` masked. Login is out of scope for this command: a capability
    aimed at a route behind a login gate must itself declare the login steps.
    """
    if mode not in get_args(Mode):
        # E32: `Mode` is a closed literal in the engine's type, but a `--mode` value is
        # user text and nothing narrowed it at this boundary -- `--mode bogus` used to
        # sail past the `supervised` check into an unattended embedded replay. Refused
        # first, before any I/O, in the same shape as the `supervised` refusal below.
        typer.echo(
            f"--mode must be one of {', '.join(get_args(Mode))}; got {mode!r}", err=True
        )
        raise typer.Exit(code=1)
    if mode == "supervised":
        # E20: a client-side fast-path in front of the same `NotImplementedError` the
        # engine would raise anyway -- checked first, before any I/O, so a caller does
        # not pay for a browser launch (or even an artifact load) it already knows will
        # fail.
        typer.echo("supervised mode is not yet implemented", err=True)
        raise typer.Exit(code=1)

    # E12: the deployment allowlist is loaded before the artifact -- a replay never runs
    # without one, and a refusal here is the same one-line, exit-2 shape as a load refusal
    # (no evidence: nothing has been run).
    try:
        policy = load_policy(policy_path)
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    artifact: Artifact
    try:
        artifact, _findings = load(artifact_id, version, root)
    except (FileNotFoundError, ValueError) as exc:
        # A missing artifact or a load-time refusal (error-level findings,
        # FORBIDDEN_CONTENT, an id/version mismatch) is not a business outcome and not
        # an input problem -- `load`'s own message already names the finding codes, so
        # it is printed as-is rather than wrapped. Exit 2, distinct from a `Failure`'s 1,
        # to tell "the artifact could not even be loaded" apart from "it loaded and the
        # replay failed". No evidence is written: there is no artifact to record a run of.
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    # E6: criterion 4 at the real path. `load()` has no allowlist to check narrowing
    # against; this is the first place both are in hand. Any error-level finding refuses.
    errors = [f for f in validate(artifact, policy) if f.level == "error"]
    if errors:
        typer.echo("; ".join(f"{f.code}: {f.message}" for f in errors), err=True)
        raise typer.Exit(code=2)
    effective = policy.narrowed_by(artifact.policy)
    status = (
        read_registry(root).get(artifact.id, {}).get(str(artifact.version), RegistryEntry())
        .status
    )

    # E33: the writer exists before any input is looked at, so every refusal from here on
    # leaves `result.json` under the `evidence_ref` it prints. Construction mints the run
    # id and writes nothing.
    writer = EvidenceWriter(evidence_root)

    raw_inputs = _parse_pairs(input_pairs, artifact.inputs)
    if isinstance(raw_inputs, Failure):
        _refuse(writer, raw_inputs)

    # `run.json` records the inputs as the operator supplied them (strings, sensitive
    # ones masked) as soon as they are known to be well-formed and declared -- before
    # coercion and `validate_inputs`, so a refusal on either count still leaves the
    # `run.json` + `result.json` pair an audit trail needs (E33).
    writer.write_run(
        goal=artifact.description, capability=artifact.id, inputs=dict(raw_inputs),
        input_specs=artifact.inputs, policy_mode=artifact.provenance.policy_mode,
    )

    coerced_inputs = _coerce_inputs(raw_inputs, artifact.inputs)
    if isinstance(coerced_inputs, Failure):
        _refuse(writer, coerced_inputs)
    inputs = coerced_inputs

    input_failure = validate_inputs(artifact, inputs)
    if input_failure is not None:
        # E22: refused strictly before `launch_page` is called.
        _refuse(writer, input_failure)

    # `artifact.yaml` is written only for a run that will actually execute: a refused
    # invocation ran nothing against the artifact, `run.json` already names it by id, and
    # the store holds that (id, version) immutably.
    writer.write_artifact(artifact)

    with launch_page(base_url) as page:
        surface = WebSurface(page, navigation_guard=navigation_guard(effective))
        try:
            result = run_replay(
                artifact, inputs, surface, cast(Mode, mode), deployment=effective,
                status=status, evidence=writer,
            )
        except NotImplementedError as exc:
            # Same handling as the fast-path above, for a caller that reaches this some
            # other way -- not a second definition of "not implemented".
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

    writer.write_result(
        result, redacted_outputs={name for name, spec in artifact.outputs.items() if spec.redact},
    )
    typer.echo(result.model_dump_json())
    if isinstance(result, Failure):
        raise typer.Exit(code=1)


_OPERATOR_TOKEN_ENV = "CUA_OPERATOR_TOKEN"


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Interface to bind."),
    port: int = typer.Option(8800, "--port", help="Port to bind."),
) -> None:
    """Runs the session service (spec S7): sessions, interventions, and the control lease.

    Requires `CUA_OPERATOR_TOKEN` in the environment (see `.env.example`) -- the shared
    token the intervention endpoints check -- and refuses to start without one (exit 2)
    rather than serving an operator API nobody can authenticate to, or anyone can. Session
    browsers are opened headed, because a human handoff needs a browser the operator can
    see. Binds to loopback by default.
    """
    token = os.environ.get(_OPERATOR_TOKEN_ENV, "")
    if not token:
        typer.echo(f"{_OPERATOR_TOKEN_ENV} must be set to a non-empty token", err=True)
        raise typer.Exit(code=2)
    uvicorn.run(create_session_app(operator_token=token, headless=False), host=host, port=port)


# Discovery evidence can hold page content from a real application, so unlike `replay` its
# default lives in the user's cache directory, not the current working tree.
_DISCOVER_EVIDENCE_ROOT = Path.home() / ".cache" / "cua"


def _violation(surface: WebSurface) -> str | None:
    try:
        return surface.allowlist_violation()
    except SurfaceError:
        return None


@app.command()
def discover(
    goal: str = typer.Option(..., "--goal", help="What the discovery run should accomplish."),
    root: Path = typer.Option(..., "--root", help="Artifact store root."),  # noqa: B008
    base_url: str = typer.Option(..., "--base-url", help="Base URL of the target app."),
    policy_path: Path = typer.Option(  # noqa: B008
        ..., "--policy", help="Deployment policy YAML (see policy.example.yaml)."
    ),
    evidence_root: Path = typer.Option(  # noqa: B008
        _DISCOVER_EVIDENCE_ROOT, "--evidence-root",
        help="Where evidence/<run_id>/ is written (default: per-user cache, never the repo).",
    ),
    id: str = typer.Option(..., "--id", help="The capability id to save."),  # noqa: A002
    name: str = typer.Option(..., "--name", help="The capability's human-readable name."),
    version: int = typer.Option(1, "--version"),
    input_pairs: list[str] = typer.Option(  # noqa: B008
        [], "--input", help="name=example value of a declared input, repeatable."
    ),
    verify_pairs: list[str] = typer.Option(  # noqa: B008
        [], "--verify-input",
        help="name=value used instead of the discovery value when self-verifying.",
    ),
    secret_pairs: list[str] = typer.Option(  # noqa: B008
        [], "--secret-input", help="Like --input, but the input is declared sensitive."
    ),
) -> None:
    """Drives one discovery run, compiles the trace, self-verifies it against a fresh
    session, and saves it `draft` only on success.

    The only command that needs a model API key (`GEMINI_API_KEY`, exit 2 if absent) or
    network beyond the target app (RULES.md S7). Every step is written to
    `evidence/<run_id>/trace.jsonl` as it happens, and the saved artifact's
    `Provenance.trace_ref` names that run. Never marks anything `approved` (D9): approval
    is a separate, deliberate `cua approve`. Any refusal (unfinished run, uncompilable
    trace, failed self-verification) is a one-line message and exit 1, never a traceback.
    """
    try:
        llm = RetryingClient(load_gemini_client_from_env())
    except LLMError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    try:
        policy = load_policy(policy_path)
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    declared: dict[str, DeclaredInput] = {}
    for pairs, sensitive in ((input_pairs, False), (secret_pairs, True)):
        for pair in pairs:
            key, sep, value = pair.partition("=")
            if not sep or re.fullmatch(r"[a-z][a-z0-9_]*", key) is None:
                typer.echo("an input must be name=value with name matching [a-z][a-z0-9_]*",
                           err=True)
                raise typer.Exit(code=2)
            declared[key] = DeclaredInput(
                spec=InputSpec(type="string", sensitive=sensitive), example_value=value,
            )

    verify_values = {k: d.example_value for k, d in declared.items()}
    for pair in verify_pairs:
        key, sep, value = pair.partition("=")
        if not sep or key not in declared:
            typer.echo("--verify-input must be name=value for a declared input", err=True)
            raise typer.Exit(code=2)
        verify_values[key] = value

    writer = EvidenceWriter(
        evidence_root,
        secrets=[d.example_value for d in declared.values() if d.spec.sensitive]
        + [verify_values[k] for k, d in declared.items() if d.spec.sensitive],
    )
    target = App(vendor_product=id.split(".")[0], variant="base", surface="web", entry="/")
    with launch_page(base_url) as page:
        surface = WebSurface(page, navigation_guard=navigation_guard(policy))
        # The loop observes whatever page the surface is on; a fresh browser is on a blank
        # page, so discovery is put where the capability begins, `app.entry`.
        opened = False
        with contextlib.suppress(SurfaceError):
            opened = surface.act(Action(kind="navigate", value=target.entry)).ok
        # D40: an allowlist violation outranks every other reading, so it is checked first,
        # both after the entry navigation and after the run.
        violation = _violation(surface)
        trace = None
        if violation is None and opened:
            trace = run_discover(
                goal, target, surface, policy, llm, declared_inputs=declared, evidence=writer,
            )
            violation = _violation(surface)

    if violation is not None:
        typer.echo(f"discovery refused: allowlist violation: {violation}; nothing was saved",
                   err=True)
        raise typer.Exit(code=1)
    if trace is None:
        typer.echo(f"discovery could not open entry {target.entry!r}; nothing was saved",
                   err=True)
        raise typer.Exit(code=1)
    if trace.stop_reason != "finish":
        typer.echo(
            f"discovery did not finish (stop_reason={trace.stop_reason!r}: "
            f"{trace.stop_detail}); nothing was saved", err=True,
        )
        raise typer.Exit(code=1)

    provenance = Provenance(
        discovered_at=datetime.now(UTC), model=llm.model,
        policy_mode=policy.policy_mode, provider_retention="training_permitted",
        run_id=writer.run_id, trace_ref=f"{writer.evidence_ref()}/trace.jsonl",
    )
    try:
        artifact = compile_artifact(
            trace, id=id, version=version, name=name, description=goal, app=target,
            settle=Settle(timeout_ms=5000, poll_ms=200), max_duration_ms=60000, outputs={},
            provenance=provenance,
        )
        verified = self_verify(
            artifact, base_url, inputs=dict(verify_values),
            policy=policy, evidence=writer,
        )
        path = save(verified, root)
    except (CompileError, FileExistsError, ValueError) as exc:
        typer.echo(" ".join(str(exc).split()), err=True)
        raise typer.Exit(code=1) from exc
    write_registry_entry(
        root, verified.id, verified.version, RegistryEntry(status="draft"), artifact=verified,
    )
    typer.echo(f"saved {path}, verified={verified.verified}")
