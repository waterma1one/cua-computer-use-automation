"""Deterministic fault injection.

Evidence runs must be able to demand a specific failure on any content route, on
demand, rather than waiting for the real-world condition to occur. A route opts in
with one line, calling `mockapp.app.apply_fault(request)` as its first statement; that
helper resolves the active `FaultConfig` (via `resolve_fault` below) and returns a
`Response` when a fault fired (the route must return it immediately) or `None` when the
route should proceed normally. This shape is the shared hook later routes -- including
ones that do not exist yet -- reuse rather than each writing its own fault handling.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

from fastapi import Request

FAULT_QUERY_PARAM = "fault"
FAULT_ENV_VAR = "MOCKAPP_FAULT"

# How long a "slow" fault holds the response before returning it.
SLOW_FAULT_MS = 1500


@dataclass(frozen=True)
class FaultConfig:
    """The set of injectable faults for a single request.

    At most one of these is ever true/non-zero for a given `?fault=<name>` value; the
    fields are broken out individually (rather than a single `name: str`) so a route
    can test the one condition it cares about without knowing the full name vocabulary.
    """

    not_found: bool = False
    denied: bool = False
    validation: bool = False
    expired: bool = False
    dialog: bool = False
    notice: bool = False
    slow_ms: int = 0
    error_500: bool = False


# Maps a `?fault=<name>` token to the FaultConfig field it sets. "slow" sets slow_ms to
# SLOW_FAULT_MS rather than a bool, since the field carries a duration.
_BOOL_FIELDS = ("not_found", "denied", "validation", "expired", "dialog", "notice", "error_500")
NAMES = frozenset(_BOOL_FIELDS) | {"slow"}


def _build(name: str | None) -> FaultConfig:
    if name == "slow":
        return FaultConfig(slow_ms=SLOW_FAULT_MS)
    if name in _BOOL_FIELDS:
        return FaultConfig(**{name: True})
    return FaultConfig()


def resolve_fault(request: Request) -> FaultConfig:
    """The active fault for this request: the query parameter wins, falling back to the
    per-app env var."""
    query_value = request.query_params.get(FAULT_QUERY_PARAM)
    if query_value is not None:
        return _build(query_value)
    return _build(os.environ.get(FAULT_ENV_VAR))


def maybe_delay(fault: FaultConfig) -> None:
    if fault.slow_ms:
        time.sleep(fault.slow_ms / 1000)
