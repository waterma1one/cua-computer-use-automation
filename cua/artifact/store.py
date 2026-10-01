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
   floor would defeat E4 exactly as reading past it would. So every finding, at every
   level, is logged through this module's logger, and (ruling E17) the full list is
   **always** part of `load`'s return value -- there is no opt-in that a caller could
   forget to pass. `load` returns `(artifact, findings)` unconditionally. An earlier
   revision of this module gated the findings list behind `return_findings=True`; that
   opt-in is exactly the failure E4 exists to prevent (a bare call site looks fully
   checked when it is not) and also forced a `mypy --strict`-hostile `Artifact |
   tuple[Artifact, list[Finding]]` union onto every caller, so it is gone.

This module does not decide whether a `draft` artifact may be exported as a callable tool
-- ruling E12, carried from Task 4's handoff, puts that three-part gate (`validate`
clean, `artifact.verified`, registry `status != draft`) on whoever calls
`export_tool_schema`, not on this module or that one. What this module *does* gate is
narrower and different, and (rulings E18, E19, E23) now has four parts instead of one:

1. `write_registry_entry` refuses to set `status="approved"` on an artifact that still
   carries an unverified `Expect` (acceptance criterion 6, §8.3 step 5) -- a promotion-time
   check about the artifact's own admitted state.
2. `write_registry_entry` refuses `requires_human_approval=False` wherever it cannot prove
   the flag clearable: on an artifact holding any step classified `risk="irreversible"`
   (§6.4, ruling E15), and -- ruling E15's other clause -- when no artifact can be
   resolved at all, because "nothing to check against" is not proof of anything. The flag
   defaults `True` and may be *cleared* only where §6.4 gives no rule against it AND an
   artifact was actually available to check.
3. `write_registry_entry` refuses `status="approved"` if the resolved artifact fails
   load-time validation (any `error`-level finding from `validate()`) -- ruling E19. This
   is deliberately **not** required for `status="draft"`: a fresh discovery run's file, or
   an artifact still being iterated on, must be registrable as `draft` whether or not it
   would pass `validate()` yet. Only promotion to `approved` demands that.
4. All three of the above need an artifact to inspect, and treat "resolving" and
   "validating" as two different steps (ruling E19) -- resolving an on-disk file for the
   identity/E15/expect checks never runs `validate()`'s full gate (see `_parse_artifact`),
   so a `draft` registration never depends on that gate passing; only the `approved` check
   above explicitly re-invokes it. `write_registry_entry` no longer trusts an `artifact`
   argument blindly either: passing one that does not itself claim this `(id, version)` is
   refused rather than silently trusted -- and (ruling E23) neither is a *file* trusted
   blindly: `_parse_artifact` compares the parsed `.id`/`.version` to the key it was
   resolved by and raises the same `ValueError`, on `load` and on the disk-resolution
   branch alike. A byte-copy of `corebank.probe/v1.yaml` at `spoofed.capability/v9.yaml`
   loaded and approved under the spoofed key before that (C4). See
   `write_registry_entry`'s own docstring for the exact resolution order (ruling E18) and
   why: an artifact argument naming a different `(id, version)` than the call, an
   `approved` entry for an `(id, version)` this store holds no file for at all, and a
   `requires_human_approval=False` entry with nothing to check it against, were all silent
   holes in the original gate, closed here.

**Criterion 1 is enforced by `validate()`, and this module inherits it (ruling E22).** A
hostname, an IP address, a URL scheme, a CSS selector, an XPath, or a credential literal
must never reach the artifact file, whichever field it arrives through. Ruling E16 first
put that check inside `save`, which closed the write path and nothing else: a file
hand-edited on disk loaded clean and could be approved by id (C3). The detectors now live
in `cua.artifact.validate` as the `FORBIDDEN_CONTENT` finding family, so `load` refuses
through its existing error-level rule, the approval gate refuses through
`_load_time_errors`, and `save` refuses on `CRITERION_1_CODES` -- that family only, not
every error, because E19 deliberately lets a draft carrying `RISK_UNCLASSIFIED` be saved
and registered. One rule, one place, three doors.

**Writes are atomic (ruling E24, M4).** `save` writes to a sibling temp file in the same
directory and `os.replace`s it into place, so a crash mid-write leaves no partial
`v<N>.yaml` for the immutability check to refuse forever; the registry is written the same
way. **The file carries §4.1's shape (M5):** `None`-valued optionals are omitted rather
than written as `pattern: null`, `scope: null`, and read back as `None`. **The registry is
rewritten through `RegistryEntry` for every entry (M3)**, so it can never be written in a
shape `read_registry` refuses.

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
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from cua.artifact.models import Artifact, RegistryStatus
from cua.artifact.validate import CRITERION_1_CODES, Finding, validate

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
    remember to lock it down. Ruling E15 makes half of that asymmetry a hard rule rather
    than only a default: `write_registry_entry` refuses to clear this flag to `False` on
    an artifact holding any `risk="irreversible"` step (§6.4 gives a rule there; the
    default alone is not enough). Where §6.4 gives no rule -- every other artifact -- the
    field stays a plain default a caller may clear, and computing a *tighter* value than
    `True` from an artifact's full risk classification is still a later phase's job.

    `extra="forbid"` (E5): a mistyped or misplaced key here should fail loudly, since a
    human is the one filling this out via the operator console (later phases) or by hand.
    """

    model_config = ConfigDict(extra="forbid")

    status: RegistryStatus = "draft"
    replays: int = 0
    successes: int = 0
    score: float | None = None
    requires_human_approval: bool = True
    # D66: approval and assisted-run bookkeeping. Defaulted so a registry.json written
    # before these fields existed still loads.
    approver: str | None = None
    approved_at: str | None = None
    sandbox_discovered: bool = False
    assisted_replays: int = 0


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


def _write_atomically(path: Path, text: str) -> None:
    """Writes `text` to `path` through a sibling temp file and a rename (ruling E24, M4).

    The temp file lives in `path`'s own directory so the final `os.replace` is a same-
    filesystem rename, which is atomic on every platform this runs on: a reader sees the
    old file or the whole new one, never a prefix. A failure at any point -- the write, or
    the rename -- removes the temp file, so a crash leaves nothing behind at the final path
    and no litter beside it. Before this, `save` used a bare `write_text`, and a crash
    mid-write left a truncated `v<N>.yaml` that the immutability check then refused to
    overwrite forever.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def dump_yaml(artifact: Artifact) -> str:
    """Renders `artifact` to the exact YAML text `save` writes to disk.

    `mode="json"` (so `datetime`, and every nested Pydantic model, becomes plain
    YAML-representable data), `exclude_none=True` (ruling E24, M5: a `None`-valued
    optional is omitted, as §4.1's worked example omits it, rather than written as
    `pattern: null`), and `sort_keys=False`, so the text reads in the field order §4.1's
    shape declares -- a human reviews this file, and `schema_version` before `id` before
    `version` is the order the spec itself writes them in, which alphabetical sorting
    would scramble.

    Factored out of `save` so any other writer of an artifact's YAML representation --
    `cua.observability.EvidenceWriter.write_artifact` copies `artifacts/<id>/v<version>.yaml`
    verbatim into a run's evidence directory -- shares this one implementation rather than
    growing a second copy of the same dump logic that can drift from it.
    """
    data: dict[str, Any] = artifact.model_dump(mode="json", exclude_none=True)
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


def save(artifact: Artifact, root: Path) -> Path:
    """Writes `artifact` to `artifacts/<id>/v<version>.yaml` under `root`, immutably.

    Refuses (`FileExistsError`) to overwrite an existing version file -- §4.1: the
    artifact file is immutable, and a new revision is a new version number, never an
    in-place edit. Refuses (`ValueError`, rulings E16 and E22) to write an artifact on
    which `validate()` reports any finding in acceptance criterion 1's family
    (`CRITERION_1_CODES`: a hostname, IP address, URL scheme, CSS selector or XPath in any
    string field, a credential literal written into a protected control, or a literal on a
    step with no locator to check it against). Only that family: E19 lets a draft with
    `RISK_UNCLASSIFIED` be saved and registered, and `load` is where every error-level
    finding is refused. Both checks run before any directory is created or any byte
    written, so a refused save leaves the store untouched.

    Written with `mode="json"` (so `datetime`, and every nested Pydantic model, becomes
    plain YAML-representable data), `exclude_none=True` (ruling E24, M5: a `None`-valued
    optional is omitted, as §4.1's worked example omits it, rather than written as
    `pattern: null`; an omitted optional parses back to `None`, which the round-trip test
    pins) and `sort_keys=False`, so the file reads in the field order §4.1's shape declares
    -- a human reviews this file, and `schema_version` before `id` before `version` is the
    order the spec itself writes them in, which alphabetical sorting would scramble. The
    bytes reach `path` through `_write_atomically`, so a crash never leaves a partial file.
    """
    path = _artifact_path(root, artifact.id, artifact.version)
    if path.exists():
        raise FileExistsError(
            f"{path} already exists; artifacts/<id>/v<version>.yaml is immutable -- "
            f"write a new version instead of overwriting this one"
        )
    violations = [f for f in validate(artifact) if f.code in CRITERION_1_CODES]
    if violations:
        raise ValueError(
            f"refusing to save {artifact.id} v{artifact.version}: the artifact carries "
            f"content acceptance criterion 1 forbids: "
            + "; ".join(f"{f.code}: {f.message}" for f in violations)
        )
    _write_atomically(path, dump_yaml(artifact))
    return path


def _parse_artifact(root: Path, id: str, version: int) -> Artifact:
    """Reads and parses `artifacts/<id>/v<version>.yaml` into an `Artifact` -- structural
    parsing only, no `validate()` call -- and checks that the file is what its path says.

    Factored out of `load` (ruling E19) so `write_registry_entry` can resolve an on-disk
    file for its identity/expect/irreversible-step checks without going through `load`'s
    load-time-validation gate: a `draft` registration must never depend on the artifact
    passing `validate()` -- only a promotion to `approved` does, and that gate calls
    `validate()` explicitly for itself (see `write_registry_entry`).

    Ruling E23: the parsed `.id` and `.version` must equal the key the file was resolved
    by, or this raises the same `ValueError` the passed-artifact path of
    `write_registry_entry` raises. Identity is checked wherever a file is resolved by key,
    because a file whose content disagrees with its path is a defect in every reading: E18
    compared only a *passed* artifact to the key, so a byte-copy of one version's file at
    another key's path loaded and approved under the spoofed key (C4).

    A file that is not parseable YAML at all is a malformed artifact like any other, and is
    refused as `ValueError` with the parser's own complaint in the message. `yaml.YAMLError`
    is not a `ValueError`, so left uncaught a hand-edited file with an unclosed bracket
    escaped every caller's `except (FileNotFoundError, ValueError)` as a traceback.
    """
    path = _artifact_path(root, id, version)
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(f"{path} could not be parsed: {exc}") from exc
    artifact = Artifact.model_validate(raw)
    if artifact.id != id or artifact.version != version:
        raise ValueError(
            f"{path} declares itself {artifact.id!r} v{artifact.version}, which does not "
            f"match the path it was resolved by ({id!r}, v{version}); a file whose "
            f"content disagrees with its location is refused rather than trusted"
        )
    return artifact


def _load_time_errors(findings: list[Finding]) -> list[Finding]:
    """The `error`-level findings among `findings` -- the one definition of "what counts as
    invalid", shared by `load` and by `write_registry_entry`'s approval gate (ruling E19).

    Takes the findings rather than the artifact so each caller runs `validate()` exactly
    once and can log the full list (`_log_findings`) before filtering it.
    """
    return [f for f in findings if f.level == "error"]


def load(id: str, version: int, root: Path) -> tuple[Artifact, list[Finding]]:
    """Reads back `artifacts/<id>/v<version>.yaml` under `root`, validating on the way out.

    Raises `FileNotFoundError` if the version does not exist, and `ValueError` if the
    file's own `id`/`version` disagree with the ones asked for (ruling E23, via
    `_parse_artifact`). Runs `validate()` (with no `DeploymentAllowlist` -- this store has
    no way to obtain one; every load therefore carries Task 2's `ALLOWLIST_NOT_CHECKED`
    note) and raises `ValueError` if any finding is `error`-level, so a caller can never
    receive an `Artifact` back that the validator considers broken. Acceptance criterion 1
    is among those errors (`FORBIDDEN_CONTENT`, ruling E22), so a file hand-edited on disk
    to carry a hostname is refused here exactly as `save` would have refused it.

    Always returns `(artifact, findings)` (ruling E17) -- there is no opt-in call shape
    that hands back a bare `Artifact` looking fully checked when it is not. Every finding,
    at every level, is also logged before this function returns or raises, so a caller
    that only wants the artifact and never inspects the second element still gets the full
    picture in the log.
    """
    path = _artifact_path(root, id, version)
    if not path.exists():
        raise FileNotFoundError(f"no artifact at {path}")
    artifact = _parse_artifact(root, id, version)

    findings = validate(artifact)
    _log_findings(findings, artifact_id=id, version=version)
    errors = _load_time_errors(findings)
    if errors:
        summary = "; ".join(f"{f.code}: {f.message}" for f in errors)
        raise ValueError(
            f"{id} v{version} failed load-time validation with {len(errors)} error(s): "
            f"{summary}"
        )

    return artifact, findings


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

    Walks every step and every expect on it (not `steps[0]` or `expects[0]` alone, per
    ruling I6): an unverified expect on a later step, or a later expect on a step that
    also carries an earlier, verified one, blocks approval exactly the same way.
    """
    return any(expect.verified is False for step in artifact.steps for expect in step.expects)


def _has_irreversible_step(artifact: Artifact) -> bool:
    """§6.4 / ruling E15: does any step of `artifact` carry `risk="irreversible"`?

    Walks every step, not just `steps[0]` -- an irreversible action anywhere in the
    capability makes `requires_human_approval=False` un-clearable for it.
    """
    return any(step.risk == "irreversible" for step in artifact.steps)


def write_registry_entry(
    root: Path, id: str, version: int, entry: RegistryEntry, *, artifact: Artifact | None = None
) -> None:
    """Writes `entry` into `artifacts/registry.json` at `(id, version)`, creating or
    replacing that one entry -- every other `(id, version)` already registered is left
    untouched.

    `artifact` is optional -- an operator may flip a registry entry's status without this
    call having the compiled artifact in hand (e.g. from the console, by id and version
    alone). It is an **optimisation** for a caller that already holds the artifact it just
    compiled, not a way to skip the gates below (ruling E18); passing an artifact that does
    not itself claim this `(id, version)` is refused rather than silently trusted.

    Resolution order (ruling E18), before any gate runs:

    1. If `artifact` is given, its `.id`/`.version` MUST equal the `id`/`version`
       arguments, or this raises `ValueError` -- closes I1's identity hole (an artifact
       for a different capability being used to clear this one's gates).
    2. Otherwise, if `artifacts/<id>/v<version>.yaml` exists, it is **parsed**
       (`_parse_artifact`, structural only -- deliberately not `load`, ruling E19): a
       `draft` registration must not depend on the file passing `validate()`. The file's
       own `id`/`version` must match the key, or this raises the same `ValueError` as
       step 1 (ruling E23) -- whatever `status` is being written.
    3. Otherwise, nothing is resolved -- there is no artifact anywhere to check.

    With something resolved, regardless of which of the two resolution paths produced it:

    - `status="approved"` is refused if the artifact holds any unverified `Expect`
      (acceptance criterion 6, §8.3 step 5), or if it fails load-time validation --
      any `error`-level finding from `validate()` (ruling E19; `_load_time_errors` is the
      same filter `load` applies, not a second definition of what counts as an error).
      Every finding the gate saw is logged through `_log_findings` first, whichever path
      resolved the artifact, so approval by id carries the same log trail `load` does.
      Neither check runs for `status="draft"`: an artifact still being iterated on, or one
      nobody has finished classifying yet, must still be registrable as a draft.
    - `requires_human_approval=False` is refused if the artifact holds any
      `risk="irreversible"` step (§6.4, ruling E15).

    With nothing resolved:

    - `status="approved"` is refused outright -- the store cannot approve a capability it
      does not hold any record of (closes I1's "ghost id" hole).
    - `requires_human_approval=False` is also refused (ruling E15's fallback clause): with
      no artifact to check, the store cannot prove no step is irreversible, and silently
      keeping the entry at `True` instead of raising would hide the caller's explicit
      disagreement with the store rather than surface it.
    - `status="draft"` with the field left at its default still writes; a fresh discovery
      run must be registrable before it has been compiled to disk or handed back to this
      call.

    The registry itself is read back through `read_registry` and every entry -- the one
    being written and every untouched one -- is re-serialised from its `RegistryEntry`
    (ruling E24, M3), so an entry `read_registry` would refuse (an unknown key, a bad
    status) is refused here rather than copied forward; the write is atomic (M4).
    """
    resolved: Artifact | None
    if artifact is not None:
        if artifact.id != id or artifact.version != version:
            raise ValueError(
                f"the supplied artifact is {artifact.id!r} v{artifact.version}, which "
                f"does not match the registry key ({id!r}, v{version}); "
                f"write_registry_entry only accepts the artifact actually being "
                f"registered under its own identity"
            )
        resolved = artifact
    elif _artifact_path(root, id, version).exists():
        resolved = _parse_artifact(root, id, version)
    else:
        resolved = None

    if resolved is not None:
        if entry.status == "approved":
            if _has_unverified_expect(resolved):
                raise ValueError(
                    f"{id} v{version} cannot be marked approved: it holds at least one "
                    f"unverified expect (§8.3 step 5 -- the compiler cannot invent "
                    f"knowledge of a state it never observed)"
                )
            findings = validate(resolved)
            _log_findings(findings, artifact_id=id, version=version)
            errors = _load_time_errors(findings)
            if errors:
                summary = "; ".join(f"{f.code}: {f.message}" for f in errors)
                raise ValueError(
                    f"{id} v{version} cannot be marked approved: it fails load-time "
                    f"validation with {len(errors)} error(s): {summary}"
                )
        if entry.requires_human_approval is False and _has_irreversible_step(resolved):
            raise ValueError(
                f"{id} v{version} cannot clear requires_human_approval: it holds at "
                f"least one step classified risk='irreversible' (§6.4) -- an irreversible "
                f"capability may not run unattended, so this flag cannot be turned off "
                f"for it"
            )
    else:
        if entry.status == "approved":
            raise ValueError(
                f"{id} v{version} cannot be marked approved: no artifact is on disk for "
                f"this (id, version) and none was supplied to check against -- the store "
                f"cannot approve a capability it does not hold"
            )
        if entry.requires_human_approval is False:
            raise ValueError(
                f"{id} v{version} cannot clear requires_human_approval: no artifact is on "
                f"disk for this (id, version) and none was supplied to check against -- "
                f"the store cannot prove no step is irreversible, so the flag stays "
                f"un-clearable until it can be checked (§6.4, ruling E15)"
            )

    registry = read_registry(root)
    registry.setdefault(id, {})[str(version)] = entry
    serialised = {
        artifact_id: {
            version_key: version_entry.model_dump(mode="json")
            for version_key, version_entry in versions.items()
        }
        for artifact_id, versions in registry.items()
    }
    _write_atomically(_registry_path(root), json.dumps(serialised, indent=2, sort_keys=True))
