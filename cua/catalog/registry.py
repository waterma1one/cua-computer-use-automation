"""Discovery, approval and stability bookkeeping over `artifacts/` and `registry.json`.

Status and stability live in the registry, keyed `(id, version)`; the artifact file is
immutable (spec S4.1). A capability with no registry entry is a draft.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cua.artifact.models import Artifact, RegistryStatus
from cua.artifact.schema import export_tool_schema
from cua.artifact.store import RegistryEntry, load, read_registry, write_registry_entry
from cua.artifact.validate import Finding
from cua.catalog.tools import ToolDefinition, build_tool_definition
from cua.replay.result import ReplayResult, Success

logger = logging.getLogger(__name__)

_VERSION_FILE = re.compile(r"^v(\d+)\.yaml$")

# (artifact path, mtime_ns) -> the exported schema, so describe() hands back the same dict
# every time until the file changes (D63).
_SCHEMA_CACHE: dict[tuple[str, int], dict[str, Any]] = {}


class CatalogRefusal(Exception):
    """The catalog declines to run or describe something. The message says what to do."""


def _versions(root: Path, id: str) -> list[int]:
    directory = root / "artifacts" / id
    found = set()
    if directory.is_dir():
        for path in directory.iterdir():
            match = _VERSION_FILE.match(path.name)
            if match:
                found.add(int(match.group(1)))
    return sorted(found)


def _ids(root: Path) -> list[str]:
    base = root / "artifacts"
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and _versions(root, p.name))


def entry_for(root: Path, id: str, version: int) -> RegistryEntry:
    return read_registry(root).get(id, {}).get(str(version), RegistryEntry())


def resolve_version(root: Path, id: str, version: int | None = None) -> int:
    """Latest approved version, else the latest draft; `version` overrides (D63)."""
    versions = _versions(root, id)
    if not versions:
        raise CatalogRefusal(f"no capability {id!r} in the catalog")
    if version is not None:
        if version not in versions:
            raise CatalogRefusal(f"capability {id!r} has no version {version}")
        return version
    registered = read_registry(root).get(id, {})
    approved = [v for v in versions if registered.get(str(v), RegistryEntry()).status == "approved"]
    return max(approved) if approved else max(versions)


def _schema_for(root: Path, id: str, version: int, artifact: Artifact) -> dict[str, Any]:
    path = root / "artifacts" / id / f"v{version}.yaml"
    key = (str(path.resolve()), path.stat().st_mtime_ns)
    if key not in _SCHEMA_CACHE:
        _SCHEMA_CACHE[key] = export_tool_schema(artifact)
    return _SCHEMA_CACHE[key]


def load_gated(root: Path, id: str, version: int) -> tuple[Artifact, list[Finding]]:
    try:
        return load(id, version, root)
    except (FileNotFoundError, ValueError) as exc:
        raise CatalogRefusal(str(exc)) from exc


def describe(root: Path, id: str, version: int | None = None) -> ToolDefinition:
    resolved = resolve_version(root, id, version)
    artifact, findings = load_gated(root, id, resolved)
    status: RegistryStatus = entry_for(root, id, resolved).status
    return build_tool_definition(
        artifact, _schema_for(root, id, resolved, artifact), status=status,
        allowlist_checked=not any(f.code == "ALLOWLIST_NOT_CHECKED" for f in findings),
    )


def list_capabilities(root: Path) -> list[ToolDefinition]:
    """One tool definition per capability id, at its resolved version (D63). An artifact
    that fails to load is logged and left out rather than hiding the rest."""
    tools = []
    for id in _ids(root):
        try:
            tools.append(describe(root, id))
        except CatalogRefusal as exc:
            logger.warning("catalog skips %s: %s", id, exc)
    return tools


def approve(
    root: Path, id: str, version: int, approver: str, *, now: datetime | None = None
) -> RegistryEntry:
    """Marks `(id, version)` approved, recording who approved it and whether it was
    discovered in sandbox mode (S6.2). Replay counts and score are preserved."""
    if not approver.strip():
        raise ValueError("approve needs a non-empty approver")
    artifact, _findings = load(id, version, root)
    stamp = (now or datetime.now(UTC)).isoformat()
    entry = entry_for(root, id, version).model_copy(update={
        "status": "approved", "approver": approver.strip(), "approved_at": stamp,
        "sandbox_discovered": artifact.provenance.policy_mode == "sandbox",
    })
    write_registry_entry(root, id, version, entry, artifact=artifact)
    return entry


def record_replay(root: Path, id: str, version: int, result: ReplayResult) -> RegistryEntry:
    """D66: only `assistance == "none"` successes count. An assisted success bumps
    `assisted_replays` only; a business outcome or failure is an unassisted replay with
    no success. `score = successes / replays`."""
    entry = entry_for(root, id, version)
    update: dict[str, Any]
    if isinstance(result, Success) and result.assistance != "none":
        update = {"assisted_replays": entry.assisted_replays + 1}
    else:
        replays = entry.replays + 1
        successes = entry.successes + (1 if isinstance(result, Success) else 0)
        update = {"replays": replays, "successes": successes, "score": successes / replays}
    updated = entry.model_copy(update=update)
    write_registry_entry(root, id, version, updated)
    return updated
