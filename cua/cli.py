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
from typing import cast

import typer

from cua.artifact.models import Artifact, InputSpec
from cua.artifact.store import load
from cua.observability.evidence import EvidenceWriter
from cua.replay.engine import replay as run_replay
from cua.replay.engine import validate_inputs
from cua.replay.result import Failure, Mode
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


def _parse_inputs(pairs: list[str], input_specs: dict[str, InputSpec]) -> dict[str, object]:
    """Parses repeated `KEY=VALUE` pairs into a `dict[str, str]`, then coerces any value
    whose declared `InputSpec.type == "integer"` to `int` (E20).

    Every value arriving from the command line is a string; `validate_inputs` checks
    Python types directly, so an artifact declaring an integer input would otherwise be
    refused a value it should accept.
    """
    raw: dict[str, str] = {}
    for pair in pairs:
        key, _, value = pair.partition("=")
        raw[key] = value

    coerced: dict[str, object] = {}
    for key, value in raw.items():
        spec = input_specs.get(key)
        if spec is not None and spec.type == "integer":
            coerced[key] = int(value)
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
    `Failure` or when `--mode supervised` is requested (unattended replay only, this
    phase). Login is out of scope for this command: a capability aimed at a route behind
    a login gate must itself declare the login steps.
    """
    if mode == "supervised":
        # E20: a client-side fast-path in front of the same `NotImplementedError` the
        # engine would raise anyway -- checked first, before any I/O, so a caller does
        # not pay for a browser launch (or even an artifact load) it already knows will
        # fail.
        typer.echo("supervised mode is not yet implemented", err=True)
        raise typer.Exit(code=1)

    artifact: Artifact
    artifact, _findings = load(artifact_id, version, root)
    inputs = _parse_inputs(input_pairs, artifact.inputs)

    input_failure = validate_inputs(artifact, inputs)
    if input_failure is not None:
        # E22: refused before `EvidenceWriter` is constructed or `launch_page` is called.
        typer.echo(input_failure.model_dump_json())
        raise typer.Exit(code=1)

    writer = EvidenceWriter(evidence_root)
    # E16: both artifacts of the run's identity are on disk before a page ever opens.
    writer.write_run(
        goal=artifact.description, capability=artifact.id, inputs=inputs,
        input_specs=artifact.inputs, policy_mode=artifact.provenance.policy_mode,
    )
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
