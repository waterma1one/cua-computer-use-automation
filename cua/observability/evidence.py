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
from pathlib import Path

from cua.artifact.models import Artifact, InputSpec
from cua.artifact.store import dump_yaml
from cua.observability.log import RunLog
from cua.replay.result import ReplayResult, mint_run_id
from cua.surface.models import EvidenceFrame
from cua.surface.snapshot import scrub_protected_values

_REDACTION_MARKER = "[REDACTED]"


class EvidenceWriter:
    """Writes one run's evidence under `root/evidence/<run_id>/`.

    `run_id` is minted once, at construction (`mint_run_id()`), and is stable for the
    life of this writer -- every method below writes under the same run directory.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self.run_id = mint_run_id()

    def _run_dir(self) -> Path:
        return self._root / "evidence" / self.run_id

    def evidence_ref(self) -> str:
        """A pointer into this run's evidence trail, stored on every `ReplayResult`."""
        return f"evidence/{self.run_id}"

    def event(self, **fields: object) -> None:
        """Delegates to an internal `RunLog` at `evidence/<run_id>/trace.jsonl`."""
        RunLog(self._run_dir() / "trace.jsonl").event(**fields)

    def frame(self, frame: EvidenceFrame, name: str) -> None:
        """Writes one `EvidenceFrame` under `name` (E16: both artifacts in one call, since
        the engine always has both a screenshot and a snapshot at once).

        `screenshots/<name>.png` is written only when `frame.image_png` is not `None`.
        `snapshots/<name>.yaml` is always written, re-scrubbed through
        `scrub_protected_values` -- belt-and-suspenders with `WebSurface.capture()`
        already scrubbing it before this frame ever reached here.
        """
        run_dir = self._run_dir()
        if frame.image_png is not None:
            screenshots_dir = run_dir / "screenshots"
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            (screenshots_dir / f"{name}.png").write_bytes(frame.image_png)

        snapshots_dir = run_dir / "snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        scrubbed = scrub_protected_values(frame.snapshot_yaml)
        (snapshots_dir / f"{name}.yaml").write_text(scrubbed)

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
        """
        masked_inputs: dict[str, object] = {
            name: _REDACTION_MARKER if input_specs.get(name, InputSpec(type="string")).sensitive
            else value
            for name, value in inputs.items()
        }
        data = {
            "goal": goal,
            "capability": capability,
            "inputs": masked_inputs,
            "policy_mode": policy_mode,
        }
        run_dir = self._run_dir()
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(json.dumps(data, indent=2))

    def write_result(self, result: ReplayResult) -> None:
        """Writes `result.json`: the `ReplayResult` exactly as returned to the caller."""
        run_dir = self._run_dir()
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "result.json").write_text(json.dumps(result.model_dump(mode="json"), indent=2))

    def write_artifact(self, artifact: Artifact) -> None:
        """Writes `artifact.yaml`, via `cua.artifact.store.dump_yaml` -- never a second
        dump implementation."""
        run_dir = self._run_dir()
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "artifact.yaml").write_text(dump_yaml(artifact))
