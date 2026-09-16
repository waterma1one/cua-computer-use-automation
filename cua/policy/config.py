"""The deployment policy file: spec §6.1's per-instance allowlist plus §6.2's `policy_mode`,
as YAML. Configuration, never the artifact.

`PolicyConfig` *is* a `DeploymentAllowlist` (E1) -- the validator's narrowing check, the
replay engine's gates and the surface's navigation guard all take the base type, so one
loaded file serves every consumer without a conversion step that could drift.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import ValidationError

from cua.artifact.validate import DeploymentAllowlist

__all__ = ["PolicyConfig", "PolicyMode", "load_policy"]

PolicyMode = Literal["strict", "sandbox"]


class PolicyConfig(DeploymentAllowlist):
    """A `DeploymentAllowlist` with the discovery-time `policy_mode` (§6.2). `strict`
    escalates every risky step during discovery; `sandbox` auto-allows them against a
    declared mock target and marks the artifact ineligible for automatic promotion. Parsed
    and carried here; phase 7's discovery loop is its consumer.
    """

    policy_mode: PolicyMode = "strict"


def load_policy(path: Path) -> PolicyConfig:
    """Reads and validates a policy file. `FileNotFoundError` if it does not exist;
    `ValueError` (one message, field names included) for a document that is not a mapping
    or does not validate -- `extra="forbid"` is inherited, so a misspelt key is refused
    rather than silently permitting nothing."""
    if not path.exists():
        raise FileNotFoundError(f"no policy file at {path}")
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(f"policy file {path} could not be parsed: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"policy file {path} must be a mapping at the top level")
    try:
        return PolicyConfig.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"policy file {path} is invalid: {exc}") from exc
