"""Deterministic fault injection.

Evidence runs must be able to demand a specific failure on any gated content route, on
demand, rather than waiting for the real-world condition to occur. A route opts in
with one line, calling `mockapp.app.apply_fault(request)` as its first statement; that
helper resolves the active `FaultConfig` (via `resolve_fault` below) and returns a
`Response` when a fault fired (the route must return it immediately) or `None` when the
route should proceed normally. This shape is the shared hook later routes -- including
ones that do not exist yet -- reuse rather than each writing its own fault handling.
`/`, `/nav`, and both `/login` routes are not gated and do not call it.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, fields

from fastapi import HTTPException, Request

FAULT_QUERY_PARAM = "fault"
FAULT_ENV_VAR = "MOCKAPP_FAULT"

# How long a "slow" fault holds the response before returning it. 1500ms is the default
# so the delay is actually observable to a human watching a live browser run; a test
# that needs the fault to fire without paying that cost overrides it with
# MOCKAPP_SLOW_FAULT_MS, read the same lazy, per-call way as FAULT_ENV_VAR above so a
# monkeypatched env var takes effect on the very next request.
SLOW_FAULT_MS = 1500
SLOW_FAULT_MS_ENV = "MOCKAPP_SLOW_FAULT_MS"


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
# SLOW_FAULT_MS rather than a bool, since the field carries a duration. Derived from the
# dataclass's own fields (rather than hand-maintained) so a future bool field added to
# FaultConfig is picked up automatically instead of silently staying unreachable by name.
_BOOL_FIELDS = tuple(f.name for f in fields(FaultConfig) if f.type == "bool")
NAMES = frozenset(_BOOL_FIELDS) | {"slow"}


def _build(name: str | None) -> FaultConfig:
    if not name:
        return FaultConfig()
    if name not in NAMES:
        # An unrecognized non-empty fault name must fail loudly, not silently render a
        # normal page: for a fixture whose entire job is reproducible evidence, a typo
        # that quietly produces a green result is the worst possible failure mode. An
        # HTTPException surfaces as a plain, unambiguous error response rather than a
        # bare crash, and is deliberately distinct from the 500 `error_500` itself
        # returns, so the two are never confused for one another.
        known = ", ".join(sorted(NAMES))
        raise HTTPException(
            status_code=400,
            detail=f"mockapp: unrecognized fault {name!r} (expected one of: {known})",
        )
    if name == "slow":
        slow_ms = int(os.environ.get(SLOW_FAULT_MS_ENV) or SLOW_FAULT_MS)
        return FaultConfig(slow_ms=slow_ms)
    return FaultConfig(**{name: True})


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
