"""The `cua` command-line entry point.

Only `cua discover` (a later phase) requires a model API key; this module's `replay`
command runs with no key and no network beyond the target application (RULES.md S7).

This module never names the driver package directly. Chromium is launched behind
`cua.surface.web.launch_page`, the one factory in the system permitted to do that, and
this module imports that name (rather than calling it fully qualified at the call site)
so a test can substitute it and have the replacement actually take effect.
"""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn, cast, get_args

import typer

from cua.artifact.models import Artifact, InputSpec
from cua.artifact.store import load
from cua.observability.evidence import EvidenceWriter
from cua.replay.engine import replay as run_replay
from cua.replay.engine import validate_inputs
from cua.replay.result import Failure, Mode, mint_run_id
from cua.surface.web import WebSurface, launch_page

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
    evidence_root: Path = typer.Option(  # noqa: B008
        Path("."), "--evidence-root", help="Where evidence/<run_id>/ is written."
    ),
    input_pairs: list[str] = typer.Option(  # noqa: B008
        [], "--input", help="KEY=VALUE, repeatable."
    ),
    mode: str = typer.Option("embedded", "--mode", help="embedded or supervised."),
) -> None:
    """Replays one capability artifact against a live target application.

    Prints the resulting `ReplayResult` as JSON to stdout and exits non-zero on a
    `Failure`, on a `--mode` that is not one of `Mode`'s literals, or when `--mode
    supervised` is requested (unattended replay only, this phase). Login is out of scope
    for this command: a capability aimed at a route behind a login gate must itself
    declare the login steps.
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
        surface = WebSurface(page)
        try:
            result = run_replay(artifact, inputs, surface, cast(Mode, mode), evidence=writer)
        except NotImplementedError as exc:
            # Same handling as the fast-path above, for a caller that reaches this some
            # other way -- not a second definition of "not implemented".
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

    writer.write_result(result)
    typer.echo(result.model_dump_json())
    if isinstance(result, Failure):
        raise typer.Exit(code=1)
