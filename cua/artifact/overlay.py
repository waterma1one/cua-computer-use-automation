"""Tenant overlay resolution (spec S4.3).

A tenant variant is an overlay file resolved onto its base artifact at load time. S4.3
states exactly what an overlay may and may not do: it may override a locator's `name` or
`surface_path`, insert a step after a named one, skip a step, and extend a step's
`expects`; it may not touch the input or output contract, "so it is rejected by the
validator rather than merged."

Two functions, matching that split:

* `validate_overlay(base, overlay)` -- never raises, returns every `Finding` it can find,
  worst first, exactly like `cua.artifact.validate.validate`. Two kinds of finding are
  overlay-specific (`OVERLAY_CHANGES_CONTRACT`, `OVERLAY_UNKNOWN_STEP`); the rest come from
  resolving the overlay and re-running `validate()` on the result, because resolution can
  produce defects (an `OUTPUT_NEVER_PRODUCED` output whose sole producer got skipped, a
  `FROM_STEP_UNKNOWN_STEP` reference left dangling by a skip) that no purely overlay-local
  check can see. The resolved artifact is held to the same seven S4.4 conditions as any
  base artifact.
* `resolve_overlay(base, overlay)` -- calls `validate_overlay` first and raises `ValueError`
  on any error-level finding, rather than handing back a half-merged artifact. S4.3's own
  words are "rejected ... rather than merged", so a caller must never receive a resolved
  artifact this module itself judged invalid.

Ruling on `Overlay.insert_after`'s shape (carried over from Task 1, settled here): Task 1
provisionally typed it `dict[str, list[Step]]`, keyed by the step id the new steps follow,
because S4.3 gives no worked YAML for an overlay file to check it against. The brief's own
test (`insert_after={"s1": [extra]}`, asserting the resolved order is `["s1", "s1b",
"s2"]`) matches that shape exactly. **Confirmed as-is; `models.py` is unchanged here.**

Ruling on `Overlay.inputs`/`Overlay.outputs` (new in Task 3, see `models.py`'s `Overlay`
docstring for the full reasoning): Task 1 shipped no such fields, relying on
`extra="forbid"` to make `Overlay(inputs=...)` impossible to construct at all. The brief's
tests require the opposite -- construction succeeds, and `validate_overlay` reports
`OVERLAY_CHANGES_CONTRACT` -- which is also S4.3's literal wording: rejected *by the
validator*, not by the parser. `Overlay` now declares both fields, defaulting to `None`
("not attempted"); neither is ever read by the resolution logic below.

Like the rest of `cua/artifact/`, nothing here imports a browser driver, references a DOM,
or carries a CSS selector or an XPath -- `tests/test_architecture.py` greps the package for
`playwright`/`selenium` and nothing else.
"""

from __future__ import annotations

from typing import Any

from cua.artifact.models import Artifact, LocatorOverride, Overlay, Step
from cua.artifact.validate import Finding, validate
from cua.surface.models import Locator

_LEVEL_ORDER = {"error": 0, "warning": 1, "note": 2}


def _sorted(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: _LEVEL_ORDER[f.level])


def _contract_findings(overlay: Overlay) -> list[Finding]:
    """S4.3's fifth condition: an overlay may not touch `inputs` or `outputs`."""
    findings: list[Finding] = []
    if overlay.inputs is not None:
        findings.append(Finding(
            level="error", code="OVERLAY_CHANGES_CONTRACT", where="inputs",
            message=(
                "an overlay may not declare inputs; that would silently change the "
                "capability's contract for every caller"
            ),
        ))
    if overlay.outputs is not None:
        findings.append(Finding(
            level="error", code="OVERLAY_CHANGES_CONTRACT", where="outputs",
            message=(
                "an overlay may not declare outputs; that would silently change the "
                "capability's contract for every caller"
            ),
        ))
    return findings


def _override_has_no_locator_findings(base: Artifact, overlay: Overlay) -> list[Finding]:
    """E10: a `locator_overrides` entry naming a step whose `locator` is `None` (a
    `navigate` step, or a mistyped target) used to apply silently -- no error, no warning,
    the step left unchanged. That is the "explicit instruction that silently does nothing"
    shape this project rejects everywhere else it has come up (`DUPLICATE_STEP_ID`,
    `LOCATOR_FALLBACK_CYCLE`). Only checked for step ids the base actually has --
    `_unknown_step_findings` already reports an id that does not exist at all, and this
    would otherwise double-report it under a second code.
    """
    by_id = {step.id: step for step in base.steps}
    findings: list[Finding] = []
    for step_id in overlay.locator_overrides:
        step = by_id.get(step_id)
        if step is not None and step.locator is None:
            findings.append(Finding(
                level="error", code="OVERLAY_OVERRIDE_HAS_NO_LOCATOR",
                where=f"locator_overrides.{step_id}",
                message=(
                    f"locator_overrides names step {step_id!r}, but that step has no "
                    f"locator to override"
                ),
            ))
    return findings


def _unknown_step_findings(base: Artifact, overlay: Overlay) -> list[Finding]:
    """Every step id an overlay names must exist in the base artifact.

    Covers all four of the overlay's step-keyed fields: a `locator_overrides`,
    `insert_after`, or `extend_expects` key naming a step the base does not have, and a
    `skip_steps` entry doing the same. One code, `OVERLAY_UNKNOWN_STEP`, because the fix is
    the same in every case: correct the id or remove the entry.
    """
    known = {step.id for step in base.steps}
    findings: list[Finding] = []

    for step_id in overlay.locator_overrides:
        if step_id not in known:
            findings.append(Finding(
                level="error", code="OVERLAY_UNKNOWN_STEP",
                where=f"locator_overrides.{step_id}",
                message=(
                    f"locator_overrides names step {step_id!r}, which the base artifact "
                    f"does not have"
                ),
            ))
    for step_id in overlay.insert_after:
        if step_id not in known:
            findings.append(Finding(
                level="error", code="OVERLAY_UNKNOWN_STEP", where=f"insert_after.{step_id}",
                message=(
                    f"insert_after names step {step_id!r}, which the base artifact does "
                    f"not have"
                ),
            ))
    for index, step_id in enumerate(overlay.skip_steps):
        if step_id not in known:
            findings.append(Finding(
                level="error", code="OVERLAY_UNKNOWN_STEP",
                where=f"skip_steps[{index}]",
                message=(
                    f"skip_steps names step {step_id!r}, which the base artifact does "
                    f"not have"
                ),
            ))
    for step_id in overlay.extend_expects:
        if step_id not in known:
            findings.append(Finding(
                level="error", code="OVERLAY_UNKNOWN_STEP",
                where=f"extend_expects.{step_id}",
                message=(
                    f"extend_expects names step {step_id!r}, which the base artifact does "
                    f"not have"
                ),
            ))
    return findings


def _apply_locator_override(locator: Locator, override: LocatorOverride) -> Locator:
    """Applies a `LocatorOverride` by reconstructing the locator through its constructor,
    not `model_copy(update=...)`.

    `Locator` carries three `model_validator`s (`role_name` requires a role, `ordinal` is
    non-negative, a fallback shares its parent's `surface_path`) that a plain
    `model_copy(update=...)` would silently skip -- exactly the `Expect` proposed/verified
    trap Task 1's handoff warned about, generalised to the one other model an overlay
    mutates. Rebuilding through `Locator(**...)` re-runs all three, so a `surface_path`
    override on a locator that carries fallbacks is still checked for surface-path
    agreement rather than silently producing an invalid locator.
    """
    updates: dict[str, Any] = {}
    if override.name is not None:
        updates["name"] = override.name
    if override.surface_path is not None:
        updates["surface_path"] = override.surface_path
    return Locator(**{**locator.model_dump(), **updates})


def _resolve(base: Artifact, overlay: Overlay) -> Artifact:
    """The resolution mechanics, assuming `overlay` has already passed
    `_contract_findings`/`_unknown_step_findings` with no errors.

    Walks `base.steps` in order, applying (in this order, per step) a locator override and
    an `expects` extension, dropping the step if it is skipped, then inserting any
    `insert_after` steps -- positionally, regardless of whether the step they follow itself
    survived a skip. Every `Expect` an overlay adds is used exactly as constructed by the
    caller: this function only concatenates lists, it never calls `model_copy` on an
    `Expect`, which is the one bypass Task 1's handoff named explicitly.
    """
    resolved_steps: list[Step] = []
    for step in base.steps:
        if step.id not in overlay.skip_steps:
            updated = step
            if step.id in overlay.locator_overrides and updated.locator is not None:
                new_locator = _apply_locator_override(
                    updated.locator, overlay.locator_overrides[step.id]
                )
                updated = updated.model_copy(update={"locator": new_locator})
            if step.id in overlay.extend_expects:
                new_expects = list(updated.expects) + list(overlay.extend_expects[step.id])
                updated = updated.model_copy(update={"expects": new_expects})
            resolved_steps.append(updated)
        if step.id in overlay.insert_after:
            resolved_steps.extend(
                inserted.model_copy(deep=True) for inserted in overlay.insert_after[step.id]
            )

    # S4.3: an overlay carries its own `verified` flag, established by self-verifying the
    # resolved base-plus-overlay against the variant it targets. A verified base says
    # nothing about the overlay, so `verified` always comes from the overlay, never a
    # carry-over from `base.verified`.
    #
    # E11: a resolved base-plus-overlay *is* the tenant variant it was resolved for, not
    # the base -- so `app.variant` becomes `overlay.targets`, never a carry-over from
    # `base.app.variant`. S4.1 types `app.variant` as `base | tenant_<x>`, exactly the
    # vocabulary `targets` already uses. The base lineage is not lost: `overlay.base_id`
    # and `overlay.base_version` still carry it.
    resolved_app = base.app.model_copy(update={"variant": overlay.targets})
    return base.model_copy(update={
        "steps": resolved_steps, "verified": overlay.verified, "app": resolved_app,
    })


def validate_overlay(base: Artifact, overlay: Overlay) -> list[Finding]:
    """Every finding this module can produce about `overlay` resolved onto `base`.

    Never raises, never short-circuits on the overlay-local checks: a caller gets the full
    list, worst first, exactly like `validate`. Resolution is only attempted -- and its
    result only re-validated -- once the overlay-local checks are clean, because resolving
    an overlay that names an unknown step or attempts to change the contract is not a
    partially meaningful operation to perform.
    """
    findings = _contract_findings(overlay)
    findings.extend(_unknown_step_findings(base, overlay))
    findings.extend(_override_has_no_locator_findings(base, overlay))
    if any(f.level == "error" for f in findings):
        return _sorted(findings)

    resolved = _resolve(base, overlay)
    findings.extend(validate(resolved))
    return _sorted(findings)


def resolve_overlay(base: Artifact, overlay: Overlay) -> Artifact:
    """Resolves `overlay` onto `base`, or raises.

    S4.3: an overlay that does not resolve cleanly "is rejected by the validator rather
    than merged." So this calls `validate_overlay` first and raises `ValueError` on any
    error-level finding instead of handing back a half-merged artifact -- a caller must
    never be able to mistake a rejected overlay's output for a usable one.
    """
    findings = validate_overlay(base, overlay)
    errors = [f for f in findings if f.level == "error"]
    if errors:
        summary = "; ".join(f"{f.code} ({f.where}): {f.message}" for f in errors)
        raise ValueError(f"overlay does not resolve cleanly onto {base.id!r}: {summary}")
    return _resolve(base, overlay)
