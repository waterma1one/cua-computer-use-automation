import pytest

from cua.artifact.models import Overlay, Step
from cua.artifact.overlay import resolve_overlay, validate_overlay
from tests.artifact.factories import base, loc


def test_an_overlay_may_override_a_locator_name() -> None:
    # The one intentional difference between the two target variants is the search field's
    # accessible name: "Member ID" in base, "Account Holder ID" in variant B.
    ov = Overlay(base_id="corebank.probe", base_version=1, targets="tenant_b",
                 locator_overrides={"s1": {"name": "Account Holder ID"}})
    resolved = resolve_overlay(base(), ov)
    assert resolved.steps[0].locator is not None
    assert resolved.steps[0].locator.name == "Account Holder ID"


def test_an_overlay_may_override_a_locator_surface_path() -> None:
    ov = Overlay(base_id="corebank.probe", base_version=1, targets="tenant_b",
                 locator_overrides={"s1": {"surface_path": [
                     {"kind": "window", "name": "main"}, {"kind": "frame", "name": "body"}]}})
    resolved = resolve_overlay(base(), ov)
    assert resolved.steps[0].locator is not None
    assert resolved.steps[0].locator.surface_path[-1].name == "body"


def test_an_overlay_may_insert_a_step_after_a_named_step() -> None:
    # Variant B inserts a branch-selection step that base does not have.
    extra = Step(id="s1b", action="click", locator=loc("Continue"), risk="safe")
    ov = Overlay(base_id="corebank.probe", base_version=1, targets="tenant_b",
                 insert_after={"s1": [extra]})
    resolved = resolve_overlay(base(), ov)
    assert [s.id for s in resolved.steps] == ["s1", "s1b", "s2"]


def test_an_overlay_may_skip_a_step() -> None:
    ov = Overlay(base_id="corebank.probe", base_version=1, targets="tenant_b",
                 skip_steps=["s1"])
    assert [s.id for s in resolve_overlay(base(), ov).steps] == ["s2"]


def test_an_overlay_may_extend_expects_without_replacing_them() -> None:
    from cua.artifact.models import Expect, Matcher
    original = base()
    original.steps[0].expects = [
        Expect(when=Matcher(role="heading", name="Member "), outcome="continue", source="observed")
    ]
    added = Expect(when=Matcher(strategy="text", name_match="contains", name="Branch required"),
                   outcome="business", code="BRANCH_REQUIRED", source="observed")
    ov = Overlay(base_id="corebank.probe", base_version=1, targets="tenant_b",
                 extend_expects={"s1": [added]})
    resolved = resolve_overlay(original, ov)
    assert len(resolved.steps[0].expects) == 2


def test_an_overlay_changing_inputs_is_rejected() -> None:
    # §4.3: changing the input or output contract silently breaks every caller.
    from cua.artifact.models import InputSpec
    ov = Overlay(base_id="corebank.probe", base_version=1, targets="tenant_b",
                 inputs={"member_id": InputSpec(type="int", required=True)})
    findings = validate_overlay(base(), ov)
    assert any(f.code == "OVERLAY_CHANGES_CONTRACT" and f.level == "error" for f in findings)


def test_an_overlay_changing_outputs_is_rejected() -> None:
    from cua.artifact.models import OutputSpec
    ov = Overlay(base_id="corebank.probe", base_version=1, targets="tenant_b",
                 outputs={"balance": OutputSpec(type="int")})
    findings = validate_overlay(base(), ov)
    assert any(f.code == "OVERLAY_CHANGES_CONTRACT" and f.level == "error" for f in findings)


def test_resolving_an_overlay_that_changes_the_contract_raises() -> None:
    from cua.artifact.models import InputSpec
    ov = Overlay(base_id="corebank.probe", base_version=1, targets="tenant_b",
                 inputs={"member_id": InputSpec(type="int", required=True)})
    with pytest.raises(ValueError):
        resolve_overlay(base(), ov)


def test_an_overlay_carries_its_own_verified_flag() -> None:
    # §4.3: a verified base says nothing about whether the overlay resolves on its variant.
    ov = Overlay(base_id="corebank.probe", base_version=1, targets="tenant_b")
    assert ov.verified is False
    resolved = resolve_overlay(base(), ov)
    assert resolved.verified is False


def test_a_verified_base_does_not_make_a_resolved_overlay_verified() -> None:
    ov = Overlay(base_id="corebank.probe", base_version=1, targets="tenant_b")
    resolved = resolve_overlay(base(verified=True), ov)
    assert resolved.verified is False


def test_an_overlay_referencing_an_unknown_step_is_rejected() -> None:
    ov = Overlay(base_id="corebank.probe", base_version=1, targets="tenant_b",
                 skip_steps=["nonexistent"])
    assert any(f.code == "OVERLAY_UNKNOWN_STEP" for f in validate_overlay(base(), ov))


# Both directions, per the project's standing rule: a check that only ever fires is as
# untrustworthy as one that never does. ORDINAL_USED shipped over-firing once already
# (mutating its guard to `any(True ...)` left 50 tests green), so every finding this module
# adds gets a matching "does not fire on a valid overlay" test.

def test_a_valid_overlay_produces_no_contract_finding() -> None:
    ov = Overlay(base_id="corebank.probe", base_version=1, targets="tenant_b",
                 locator_overrides={"s1": {"name": "Account Holder ID"}})
    findings = validate_overlay(base(), ov)
    assert not any(f.code == "OVERLAY_CHANGES_CONTRACT" for f in findings)


def test_a_valid_overlay_produces_no_unknown_step_finding() -> None:
    extra = Step(id="s1b", action="click", locator=loc("Continue"), risk="safe")
    ov = Overlay(base_id="corebank.probe", base_version=1, targets="tenant_b",
                 locator_overrides={"s1": {"name": "Account Holder ID"}},
                 insert_after={"s1": [extra]}, skip_steps=[],
                 extend_expects={"s2": []})
    findings = validate_overlay(base(), ov)
    assert not any(f.code == "OVERLAY_UNKNOWN_STEP" for f in findings)


def test_a_fully_valid_overlay_resolves_with_no_error_findings() -> None:
    ov = Overlay(base_id="corebank.probe", base_version=1, targets="tenant_b",
                 locator_overrides={"s1": {"name": "Account Holder ID"}})
    findings = validate_overlay(base(), ov)
    assert not any(f.level == "error" for f in findings)


# The handoff from Task 2: resolution itself can produce defects the overlay-specific
# checks above cannot see, so `validate_overlay` re-runs `validate()` on the resolved
# artifact and merges its findings. Two of the three named cases, exercised end to end.

def test_an_overlay_skipping_the_sole_producer_of_a_declared_output_is_rejected() -> None:
    # Skipping "s2" removes the only step that binds into the declared output "balance".
    ov = Overlay(base_id="corebank.probe", base_version=1, targets="tenant_b",
                 skip_steps=["s2"])
    findings = validate_overlay(base(), ov)
    assert any(f.code == "OUTPUT_NEVER_PRODUCED" and f.level == "error" for f in findings)
    with pytest.raises(ValueError):
        resolve_overlay(base(), ov)


def test_an_overlay_skipping_a_step_a_surviving_from_step_still_references_is_rejected() -> None:
    from cua.artifact.models import OutputSpec

    custom = base(
        outputs={"note": OutputSpec(type="string")},
        steps=[
            Step(id="s1", action="fill", locator=loc("Member ID"),
                 value={"from_input": "member_id"}, risk="safe"),
            Step(id="s2", action="read", locator=loc("Savings"), extract="text",
                 parse="money", into="_temp", risk="safe"),
            Step(id="s3", action="read", locator=loc("Note"),
                 value={"from_step": "s2"}, into="note", risk="safe"),
        ],
    )
    ov = Overlay(base_id="corebank.probe", base_version=1, targets="tenant_b",
                 skip_steps=["s2"])
    findings = validate_overlay(custom, ov)
    assert any(f.code == "FROM_STEP_UNKNOWN_STEP" and f.level == "error" for f in findings)
    with pytest.raises(ValueError):
        resolve_overlay(custom, ov)


# Fix round 1, I1: only `skip_steps` had a firing-direction test for
# `OVERLAY_UNKNOWN_STEP`. The reviewer deleted each of the other three loops in turn and
# found all 16 tests still green -- exactly the `ORDINAL_USED` over-firing shape this phase
# already hit once. One test per remaining branch, each naming an id that exists nowhere in
# `base()`'s two steps.

def test_an_unknown_step_in_locator_overrides_is_rejected() -> None:
    ov = Overlay(base_id="corebank.probe", base_version=1, targets="tenant_b",
                 locator_overrides={"nonexistent": {"name": "Account Holder ID"}})
    findings = validate_overlay(base(), ov)
    assert any(f.code == "OVERLAY_UNKNOWN_STEP" and f.level == "error" for f in findings)


def test_an_unknown_step_in_insert_after_is_rejected() -> None:
    extra = Step(id="s1b", action="click", locator=loc("Continue"), risk="safe")
    ov = Overlay(base_id="corebank.probe", base_version=1, targets="tenant_b",
                 insert_after={"nonexistent": [extra]})
    findings = validate_overlay(base(), ov)
    assert any(f.code == "OVERLAY_UNKNOWN_STEP" and f.level == "error" for f in findings)


def test_an_unknown_step_in_extend_expects_is_rejected() -> None:
    from cua.artifact.models import Expect, Matcher
    added = Expect(when=Matcher(role="heading", name="Branch required"), outcome="business",
                   source="observed")
    ov = Overlay(base_id="corebank.probe", base_version=1, targets="tenant_b",
                 extend_expects={"nonexistent": [added]})
    findings = validate_overlay(base(), ov)
    assert any(f.code == "OVERLAY_UNKNOWN_STEP" and f.level == "error" for f in findings)


# Fix round 1, I2 / controller ruling E10: a `locator_overrides` entry naming a step whose
# `locator` is `None` (a `navigate` step, or a mistyped target) applied silently -- no
# error, no warning, the step unchanged. That is the "explicit instruction that silently
# does nothing" shape this project has rejected every other time it came up.

def test_an_override_on_a_step_with_no_locator_is_rejected() -> None:
    from cua.artifact.models import Target

    custom = base(steps=[
        Step(id="s0", action="navigate", target=Target(path="/teller/index.html"),
             risk="safe"),
        Step(id="s2", action="read", locator=loc("Savings"), extract="text",
             parse="money", into="balance", risk="safe"),
    ])
    ov = Overlay(base_id="corebank.probe", base_version=1, targets="tenant_b",
                 locator_overrides={"s0": {"name": "does not matter"}})
    findings = validate_overlay(custom, ov)
    assert any(
        f.code == "OVERLAY_OVERRIDE_HAS_NO_LOCATOR" and f.level == "error" for f in findings
    )
    with pytest.raises(ValueError):
        resolve_overlay(custom, ov)


def test_an_override_on_a_step_with_a_locator_does_not_trigger_the_no_locator_finding() -> None:
    ov = Overlay(base_id="corebank.probe", base_version=1, targets="tenant_b",
                 locator_overrides={"s1": {"name": "Account Holder ID"}})
    findings = validate_overlay(base(), ov)
    assert not any(f.code == "OVERLAY_OVERRIDE_HAS_NO_LOCATOR" for f in findings)


# Fix round 1, M2: `Overlay.inputs`/`outputs` are inert today only because `_resolve` never
# reads them -- a documented convention, not a checked one. This pins it as an invariant:
# call `_resolve` directly (bypassing `validate_overlay`'s contract short-circuit, the way
# the reviewer did) and assert the resolved artifact's `inputs`/`outputs` are byte-identical
# to the base's, even though the overlay declares different ones.

def test_resolve_never_merges_an_overlays_inputs_or_outputs_into_the_result() -> None:
    from cua.artifact.models import InputSpec, OutputSpec
    from cua.artifact.overlay import _resolve

    b = base()
    ov = Overlay(base_id="corebank.probe", base_version=1, targets="tenant_b",
                 inputs={"extra": InputSpec(type="int", required=True)},
                 outputs={"extra_out": OutputSpec(type="int")})
    resolved = _resolve(b, ov)
    assert resolved.inputs == b.inputs
    assert resolved.outputs == b.outputs


# Fix round 1, M3 / controller ruling E11: a resolved base-plus-overlay *is* the tenant
# variant it was resolved for, so `app.variant` on the result is `overlay.targets`, never
# carried over from the base. §4.1 types `app.variant` as `base | tenant_<x>`, exactly the
# vocabulary `targets` already uses; the base lineage is not lost, since `base_id` and
# `base_version` on the overlay still carry it.

def test_resolved_artifact_takes_the_overlays_targets_as_its_app_variant() -> None:
    ov = Overlay(base_id="corebank.probe", base_version=1, targets="tenant_b")
    b = base()
    assert b.app.variant == "base"
    resolved = resolve_overlay(b, ov)
    assert resolved.app.variant == "tenant_b"
    # Resolving must not mutate the caller's base artifact.
    assert b.app.variant == "base"
