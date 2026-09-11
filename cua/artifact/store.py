"""The store: where the immutable artifact file and the mutable registry part company.

This is the only module in `cua/artifact/` that touches the filesystem. Everything else
in the package (`models.py`, `validate.py`, `overlay.py`, `schema.py`) is pure data
shaping; here that data is actually written to and read back from disk.

Spec §4.1's closing paragraph draws the line this module enforces: `status`
(`draft | approved`) and `stability` (`replays`, `successes`, `score`) are deliberately
**not** on `Artifact` (see that model's own docstring) and never reach
`artifacts/<id>/v<version>.yaml`. They live in `artifacts/registry.json`, keyed by
`(id, version)`, which this module reads and writes as `RegistryEntry`. The artifact file
itself is immutable once written: `save` refuses to overwrite an existing version, exactly
as §4.1 states ("a new revision is a new version number").

**How `load` surfaces findings, and why.** Task 2's handoff for this task read: "`validate`
is the load-time gate -- call it on read, and surface warnings and notes to the operator
rather than dropping them." Two things follow from that, and they are different things:

1. An `error`-level finding means the artifact must not be treated as replayable at all --
   `load` raises `ValueError` rather than handing back an `Artifact` that looks fine but
   is not.
2. A `warning` or `note` is not a defect that should stop a load (E4's whole point is that
   "not checked" must never be silently swallowed, but it is also not "invalid" -- an
   artifact with no `policy` block is still perfectly loadable). Dropping these on the
   floor on every ordinary `load()` call would defeat E4 exactly as reading past it would.
   So every finding, at every level, is logged through this module's logger before `load`
   returns or raises, and the full list is additionally available to a caller that asks
   for it via `return_findings=True` -- the catalog and console (later phases) need the
   structured list to render to an operator, not just a log line.

This module does not decide whether a `draft` artifact may be exported as a callable tool
-- ruling E12, carried from Task 4's handoff, puts that three-part gate (`validate`
clean, `artifact.verified`, registry `status != draft`) on whoever calls
`export_tool_schema`, not on this module or that one. What this module *does* gate is
narrower and different: `write_registry_entry` refuses to set `status="approved"` on an
artifact that still carries an unverified `Expect` (acceptance criterion 6, §8.3 step 5)
-- a promotion-time check about the artifact's own admitted state, not an export-time
check about the registry.

**On E11 and `app.variant`: this store keys and paths by `(id, version)` alone, never by
variant, and that is a conscious choice, not an oversight.** `resolve_overlay` (Task 3)
does not change `Artifact.id` or `Artifact.version` -- only `app.variant`, `steps`, and
`verified` -- so a resolved tenant artifact carries the *same* `(id, version)` as the base
it was resolved from. S4.3 calls a tenant variant "an overlay file resolved onto the base
artifact at load time", which this module reads as: only the base artifact is ever
persisted through `save`/`load`; a resolved variant is a transient value `resolve_overlay`
hands back in memory and is never itself passed to `save` here. Nothing in this task's
interface takes an `Overlay` or a variant, so there is no call site today that would even
attempt to save a resolved artifact under its base's `(id, version)` and collide with it.
If a later phase decides to cache a resolved variant to disk, `(id, version)` alone is not
a safe key for that -- it would silently collide with the base and with every other
variant resolved from it -- and that phase must extend the key, not this one.

Like the rest of `cua/artifact/`, nothing here imports Playwright, references a DOM, or
carries a CSS selector or an XPath.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from cua.artifact.models import Artifact
from cua.artifact.validate import Finding, validate

logger = logging.getLogger(__name__)


class RegistryEntry(BaseModel):
    """One `(id, version)`'s lifecycle state, as stored in `artifacts/registry.json`.

    `status` is exactly S4.1's `draft | approved`. `replays`, `successes`, and `score` are
    S4.1's `stability` group, flattened onto this model rather than nested under a
    `stability` field of their own -- there is nothing else in `stability` for a nested
    model to hold, and flattening keeps `RegistryEntry(status=..., replays=..., ...)`
    constructible with plain keyword arguments rather than a nested mapping literal.

    `requires_human_approval` defaults to `True`: §6.3's stated asymmetry is that
    over-classification costs a human a moment, while an irreversible action executed
    unattended cannot be undone, so an entry starts out requiring a human and must be
    affirmatively cleared, rather than starting permissive and requiring someone to
    remember to lock it down. This task only carries the field through the registry --
    computing it from an artifact's own risk classification is a later phase's job.

    `extra="forbid"` (E5): a mistyped or misplaced key here should fail loudly, since a
    human is the one filling this out via the operator console (later phases) or by hand.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["draft", "approved"] = "draft"
    replays: int = 0
    successes: int = 0
    score: float | None = None
    requires_human_approval: bool = True


def _artifact_dir(root: Path, artifact_id: str) -> Path:
    return root / "artifacts" / artifact_id


def _artifact_path(root: Path, artifact_id: str, version: int) -> Path:
    return _artifact_dir(root, artifact_id) / f"v{version}.yaml"


def _registry_path(root: Path) -> Path:
    return root / "artifacts" / "registry.json"


def _log_findings(findings: list[Finding], *, artifact_id: str, version: int) -> None:
    """Surfaces every finding through the module logger, whatever its level.

    Deliberately unconditional: an `error` is logged here too, immediately before `load`
    raises over it, so the log carries the full picture rather than just the one finding
    that happened to trip the raise.
    """
    for finding in findings:
        logger.warning(
            "%s v%s: [%s] %s%s -- %s",
            artifact_id, version, finding.level, finding.code,
            f" ({finding.where})" if finding.where else "",
            finding.message,
        )


def save(artifact: Artifact, root: Path) -> Path:
    """Writes `artifact` to `artifacts/<id>/v<version>.yaml` under `root`, immutably.

    Refuses (`FileExistsError`) to overwrite an existing version file -- §4.1: the
    artifact file is immutable, and a new revision is a new version number, never an
    in-place edit. This is checked before any directory is created or any byte written,
    so a refused save leaves the store untouched.

    Written with `mode="json"` (so `datetime`, and every nested Pydantic model, becomes
    plain YAML-representable data) and `sort_keys=False`, so the file reads in the field
    order §4.1's shape declares -- a human reviews this file, and `schema_version` before
    `id` before `version` is the order the spec itself writes them in, which alphabetical
    sorting would scramble.
    """
    path = _artifact_path(root, artifact.id, artifact.version)
    if path.exists():
        raise FileExistsError(
            f"{path} already exists; artifacts/<id>/v<version>.yaml is immutable -- "
            f"write a new version instead of overwriting this one"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = artifact.model_dump(mode="json", exclude_none=False)
    path.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
    return path


def load(
    id: str, version: int, root: Path, *, return_findings: bool = False
) -> Artifact | tuple[Artifact, list[Finding]]:
    """Reads back `artifacts/<id>/v<version>.yaml` under `root`, validating on the way out.

    Raises `FileNotFoundError` if the version does not exist. Runs `validate()` (with no
    `DeploymentAllowlist` -- this store has no way to obtain one; every load therefore
    carries Task 2's `ALLOWLIST_NOT_CHECKED` note, which is logged like every other
    finding rather than treated as a special case) and raises `ValueError` if any finding
    is `error`-level, so a caller can never receive an `Artifact` back that the validator
    considers broken.

    Every finding, at every level, is logged before this function returns or raises (see
    the module docstring for why levels below `error` are not dropped). Pass
    `return_findings=True` to additionally get the full list back for a caller -- the
    catalog and console -- that needs to render it to a human rather than just log it.
    """
    path = _artifact_path(root, id, version)
    if not path.exists():
        raise FileNotFoundError(f"no artifact at {path}")
    data = yaml.safe_load(path.read_text())
    artifact = Artifact.model_validate(data)

    findings = validate(artifact)
    _log_findings(findings, artifact_id=id, version=version)
    errors = [f for f in findings if f.level == "error"]
    if errors:
        summary = "; ".join(f"{f.code}: {f.message}" for f in errors)
        raise ValueError(
            f"{id} v{version} failed load-time validation with {len(errors)} error(s): "
            f"{summary}"
        )

    if return_findings:
        return artifact, findings
    return artifact


def read_registry(root: Path) -> dict[str, dict[str, RegistryEntry]]:
    """Reads `artifacts/registry.json`, keyed by id then by version (as a string, since
    JSON object keys are always strings).

    Returns an empty dict if the registry does not exist yet -- a fresh store with no
    entries at all is not an error; there is simply nothing registered.
    """
    path = _registry_path(root)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {
        artifact_id: {
            version: RegistryEntry.model_validate(entry) for version, entry in versions.items()
        }
        for artifact_id, versions in raw.items()
    }


def _has_unverified_expect(artifact: Artifact) -> bool:
    """Acceptance criterion 6 / §8.3 step 5: any `Expect` this artifact carries that is
    not `verified` -- regardless of `source` -- disqualifies it from `approved`.

    Not narrowed to `source == "proposed"`: a `proposed` expect is always unverified (the
    model validator on `Expect` forbids the opposite combination), so checking `verified`
    directly already covers it, and also covers an `observed`/`authored` clause that
    simply has not been verified yet -- which criterion 6's wording ("any unverified
    expect") does not exempt.
    """
    return any(expect.verified is False for step in artifact.steps for expect in step.expects)


def write_registry_entry(
    root: Path, id: str, version: int, entry: RegistryEntry, *, artifact: Artifact | None = None
) -> None:
    """Writes `entry` into `artifacts/registry.json` at `(id, version)`, creating or
    replacing that one entry -- every other `(id, version)` already registered is left
    untouched.

    When `artifact` is supplied and `entry.status == "approved"`, refuses (`ValueError`)
    to write it if the artifact holds any unverified `Expect` (acceptance criterion 6).
    `artifact` is optional -- an operator may flip a registry entry's status without this
    call having the compiled artifact in hand (e.g. from the console, by id and version
    alone) -- and when it is omitted, this gate simply does not run; there is nothing to
    inspect it against. Nothing about this function saves the artifact itself; that is
    `save`'s job, and callers that have just compiled an artifact are expected to call
    both.
    """
    if artifact is not None and entry.status == "approved" and _has_unverified_expect(artifact):
        raise ValueError(
            f"{id} v{version} cannot be marked approved: it holds at least one "
            f"unverified expect (§8.3 step 5 -- the compiler cannot invent knowledge of "
            f"a state it never observed)"
        )

    path = _registry_path(root)
    raw: dict[str, dict[str, Any]] = json.loads(path.read_text()) if path.exists() else {}
    raw.setdefault(id, {})[str(version)] = entry.model_dump(mode="json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, indent=2, sort_keys=True))
