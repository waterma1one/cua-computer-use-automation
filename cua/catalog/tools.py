"""Tool definitions: the artifact's exported JSON Schema plus catalog-level flags (D63)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cua.artifact.models import Artifact, RegistryStatus
from cua.artifact.store import has_irreversible_step


@dataclass(frozen=True)
class ToolDefinition:
    """One callable capability as the catalog advertises it.

    `schema` is the `export_tool_schema` dict, held unmodified -- not a second,
    hand-maintained copy. `irreversible` and `status` ride beside it as attributes so the
    schema stays exactly what the artifact exported. `allowlist_checked` is False whenever
    the load-time `ALLOWLIST_NOT_CHECKED` note is present: the catalog never presents an
    unchecked policy as checked and clean.
    """

    id: str
    version: int
    status: RegistryStatus
    irreversible: bool
    allowlist_checked: bool
    schema: dict[str, Any]


def build_tool_definition(
    artifact: Artifact, schema: dict[str, Any], *, status: RegistryStatus,
    allowlist_checked: bool,
) -> ToolDefinition:
    return ToolDefinition(
        id=artifact.id, version=artifact.version, status=status,
        irreversible=has_irreversible_step(artifact), allowlist_checked=allowlist_checked,
        schema=schema,
    )
