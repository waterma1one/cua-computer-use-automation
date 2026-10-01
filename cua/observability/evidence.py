"""`EvidenceWriter`: produces spec §10's on-disk evidence layout for one replay run.

    evidence/<run_id>/
      run.json        goal, capability, inputs (sensitive fields redacted), policy_mode
      trace.jsonl     one record per event -- written through an internal `RunLog`
      result.json     the `ReplayResult` exactly as returned to the caller
      artifact.yaml   a copy of the artifact this run used, via `cua.artifact.store.dump_yaml`
      screenshots/    <name>.png, on failure, on escalation, and bracketing a human window
      snapshots/      <name>.yaml, accessibility YAML, redaction-filtered before write

`EvidenceWriter` satisfies `cua.replay.engine.EvidenceSink` structurally -- this module
never imports `cua.replay.engine` (that module documents, and this package's own
docstring restates, why the coupling runs one way only).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from cua.artifact.models import Artifact, InputSpec
from cua.artifact.store import dump_yaml
from cua.observability.log import RunLog
from cua.policy.redact import RedactingWriter, mask_field, redact, redact_leaves
from cua.replay.result import ReplayResult, mint_run_id
from cua.surface.models import EvidenceFrame
from cua.surface.snapshot import scrub_protected_values

_REDACTION_MARKER = "[REDACTED]"


def mask_secrets(value: str, secrets: Iterable[str]) -> str:
    """Masks every occurrence of each secret in `value`, longest secret first so a secret
    that contains another is never left with a leaking tail. Empty secrets are skipped."""
    for secret in sorted({v for v in secrets if v}, key=len, reverse=True):
        value = value.replace(secret, _REDACTION_MARKER)
    return value


class EvidenceWriter:
    """Writes one run's evidence under `root/evidence/<run_id>/`.

    `run_id` is minted once, at construction (`mint_run_id()`), and is stable for the
    life of this writer -- every method below writes under the same run directory.
    """

    def __init__(self, root: Path, *, secrets: Iterable[str] = ()) -> None:
        self._root = root
        # Literal secret values (e.g. a discovery run's sensitive example inputs). Any
        # occurrence in an event or a snapshot is masked before it reaches disk.
        self._secrets = sorted({v for v in secrets if v}, key=len, reverse=True)
        self.run_id = mint_run_id()
        self._writer = RedactingWriter()

    def _run_dir(self) -> Path:
        return self._root / "evidence" / self.run_id

    def evidence_ref(self) -> str:
        """A pointer into this run's evidence trail, stored on every `ReplayResult`."""
        return f"evidence/{self.run_id}"

    def mask(self, text: str) -> str:
        """`text` with this writer's declared secrets masked (for messages shown to the
        operator as well as for files)."""
        return mask_secrets(text, self._secrets)

    def scrub(self, text: str) -> str:
        """`mask` plus the shape-preserving pattern filter: for free text from a page or
        an error (an observed value can be an SSN), shown to the operator or written."""
        return redact(self.mask(text))

    def _mask_secrets(self, value: object) -> object:
        if isinstance(value, str):
            return self.mask(value)
        if isinstance(value, dict):
            return {k: self._mask_secrets(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._mask_secrets(v) for v in value]
        return value

    def event(self, **fields: object) -> None:
        """Delegates to an internal `RunLog` at `evidence/<run_id>/trace.jsonl`."""
        masked = self._mask_secrets(fields)
        assert isinstance(masked, dict)
        RunLog(self._run_dir() / "trace.jsonl").event(**masked)

    def frame(self, frame: EvidenceFrame, name: str) -> None:
        """Writes one `EvidenceFrame` under `name` (E16 of phase 4: both artifacts in one
        call). `screenshots/<name>.png` is written only when `frame.image_png` is not
        `None` -- pixels, not redacted, a stated limit. `snapshots/<name>.yaml` is always
        written, re-scrubbed through `scrub_protected_values` and then through the
        `RedactingWriter`'s pattern filter (§6.5's third layer).
        """
        run_dir = self._run_dir()
        if frame.image_png is not None:
            screenshots_dir = run_dir / "screenshots"
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            (screenshots_dir / f"{name}.png").write_bytes(frame.image_png)
        scrubbed = str(self._mask_secrets(scrub_protected_values(frame.snapshot_yaml)))
        self._writer.put_text(run_dir / "snapshots" / f"{name}.yaml", scrubbed)

    def write_run(
        self, *, goal: str, capability: str, inputs: dict[str, object],
        input_specs: dict[str, InputSpec], policy_mode: str,
    ) -> None:
        """Writes `run.json`: `goal`, `capability`, `inputs` (sensitive ones masked),
        `policy_mode`.

        An input is masked with the literal `"[REDACTED]"` when its declared
        `InputSpec.sensitive` is `True` -- the same accepted global-substitution
        trade-off `scrub_protected_values` documents for snapshot YAML applies here too
        (this package's own docstring carries the note in full).

        Every key of `inputs` must have an entry in `input_specs`: an input no spec
        describes cannot be known to be non-sensitive, so it raises `KeyError` rather
        than being written in plain text by default (E31, ledger minor 10). The caller
        refuses undeclared inputs before reaching here (`cua.cli` does so as
        `INVALID_INPUT`). Nothing is written when the check fails: the comprehension
        runs to completion before `run.json` is opened.
        """
        # Non-sensitive input values still pass through the pattern filter on the way to
        # disk (E8's accepted cost, pinned by a test).
        masked_inputs: dict[str, object] = {
            name: _REDACTION_MARKER if input_specs[name].sensitive else value
            for name, value in inputs.items()
        }
        data = {
            "goal": goal,
            "capability": capability,
            "inputs": masked_inputs,
            "policy_mode": policy_mode,
        }
        self._writer.put_text(self._run_dir() / "run.json", json.dumps(data, indent=2))

    def write_discovery_run(
        self, *, goal: str, capability: dict[str, str], inputs: dict[str, object],
        input_specs: dict[str, InputSpec], policy_mode: str, base_url: str, model: str,
        tokens: dict[str, int], estimated_cost_usd: float | None, cost_note: str | None,
        step_count: int, started_at: str, ended_at: str,
    ) -> None:
        """Writes a discovery run's `run.json`: the replay fields (sensitive inputs masked
        exactly as `write_run` does) plus model, token counts, cost, step count and
        timestamps. Counts only -- never a prompt or a key."""
        data = {
            "goal": goal, "capability": capability,
            "inputs": {
                name: _REDACTION_MARKER if input_specs[name].sensitive else value
                for name, value in inputs.items()
            },
            "policy_mode": policy_mode, "base_url": base_url, "model": model,
            "tokens": tokens, "estimated_cost_usd": estimated_cost_usd,
            "cost_note": cost_note, "step_count": step_count,
            "started_at": started_at, "ended_at": ended_at,
        }
        masked = self._mask_secrets(data)
        self._writer.put_text(self._run_dir() / "run.json", json.dumps(masked, indent=2))

    def write_discovery_result(self, data: dict[str, object]) -> None:
        """Writes a discovery run's `result.json`: every string is secret-masked and then
        pattern-filtered on the way to disk."""
        masked = redact_leaves(self._mask_secrets(data))
        self._writer.put_text(self._run_dir() / "result.json", json.dumps(masked, indent=2))

    def write_result(
        self, result: ReplayResult, *, redacted_outputs: Iterable[str] = (),
    ) -> dict[str, object]:
        """Writes `result.json`: the `ReplayResult` as returned to the caller, with every
        output named in `redacted_outputs` masked through `mask_field` (E9/E16: the caller
        keeps the value; the evidence copy keeps only its shape) and the pattern filter
        applied to every string on the way to disk (E8). Returns the mapping that was
        written, so a caller can see exactly what evidence holds. The `result` object
        itself is never modified.
        """
        data = result.model_dump(mode="json")
        outputs = data.get("outputs")
        if isinstance(outputs, dict):
            for name in redacted_outputs:
                if name in outputs and outputs[name] is not None:
                    outputs[name] = mask_field(str(outputs[name]))
        written = redact_leaves(data)
        assert isinstance(written, dict)
        self._writer.put_text(self._run_dir() / "result.json", json.dumps(written, indent=2))
        return written

    def write_artifact(self, artifact: Artifact) -> None:
        """Writes `artifact.yaml`, via `cua.artifact.store.dump_yaml` -- never a second
        dump implementation -- through the pattern filter like every other text write."""
        self._writer.put_text(self._run_dir() / "artifact.yaml", dump_yaml(artifact))
