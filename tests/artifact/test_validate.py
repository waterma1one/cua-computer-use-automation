"""Spec §4.4: the load-time cross-field checks that run before any replay.

One test per condition §4.4 names, plus the ordinal warning, the unclassified-risk error
carried out of Task 1's review, and E4's skipped-narrowing note.
"""

from __future__ import annotations

from cua.artifact.models import (
    Artifact,
    CapabilityPolicy,
    InputSpec,
    OutputSpec,
    Step,
)
from cua.artifact.validate import DeploymentAllowlist, Finding, validate
from tests.artifact.factories import base, loc


def codes(artifact: Artifact) -> set[str]:
    return {f.code for f in validate(artifact)}


def errors(artifact: Artifact) -> list[Finding]:
    return [f for f in validate(artifact) if f.level == "error"]


def test_a_minimal_artifact_validates_clean() -> None:
    # The control that keeps every other test in this module honest: without it, a
    # validator that flagged everything would pass them all.
    #
    # It asserts the *whole* finding list, not just the absence of errors. Every other test
    # here checks that some code fires, so a check that fires on everything passes all of
    # them -- `ORDINAL_USED` mutated to `if any(True ...)` left fifty tests green, because
    # nothing anywhere said what a clean artifact must NOT produce. This is the one place
    # that says it, so a new code cannot be added without a deliberate edit here.
    assert [(f.level, f.code) for f in validate(base())] == [("note", "ALLOWLIST_NOT_CHECKED")]


def test_a_locator_with_no_ordinal_produces_no_ordinal_warning() -> None:
    # The over-firing direction of the ordinal check, stated on its own so the reason it
    # exists survives an edit to the control test above.
    assert "ORDINAL_USED" not in codes(base())


def test_a_from_input_naming_no_declared_input_is_an_error() -> None:
    a = base(steps=[Step(id="s1", action="fill", locator=loc("x"),
                         value={"from_input": "nonexistent"}, risk="safe")])
    assert "UNKNOWN_INPUT" in codes(a)


def test_an_into_naming_neither_an_output_nor_a_local_is_an_error() -> None:
    a = base(steps=[Step(id="s1", action="read", locator=loc("x"), extract="text",
                         into="not_declared", risk="safe")])
    assert "UNKNOWN_OUTPUT" in codes(a)


def test_an_into_naming_an_underscore_local_is_accepted() -> None:
    # §4.2 decision 9: locals carry values between steps and are never returned to the caller.
    a = base(steps=[Step(id="s1", action="read", locator=loc("x"), extract="text",
                         into="_scratch", risk="safe"),
                    Step(id="s2", action="read", locator=loc("y"), extract="text",
                         into="balance", risk="safe")])
    assert "UNKNOWN_OUTPUT" not in codes(a)


def test_a_declared_output_with_no_producing_step_is_an_error() -> None:
    a = base(outputs={"balance": OutputSpec(type="string"),
                      "orphan": OutputSpec(type="string")})
    assert "OUTPUT_NEVER_PRODUCED" in codes(a)


def test_a_from_step_cycle_is_an_error() -> None:
    a = base(steps=[Step(id="s1", action="fill", locator=loc("x"),
                         value={"from_step": "s2"}, risk="safe"),
                    Step(id="s2", action="fill", locator=loc("y"),
                         value={"from_step": "s1"}, risk="safe")])
    assert "FROM_STEP_CYCLE" in codes(a)


def test_a_from_step_naming_a_later_step_is_an_error() -> None:
    a = base(steps=[Step(id="s1", action="fill", locator=loc("x"),
                         value={"from_step": "s2"}, risk="safe"),
                    Step(id="s2", action="read", locator=loc("y"), extract="text",
                         into="balance", risk="safe")])
    assert "FROM_STEP_FORWARD_REFERENCE" in codes(a)


def test_a_forward_reference_is_not_reported_as_a_cycle() -> None:
    # The two defects have different fixes -- reorder the steps, versus break the loop --
    # so they are separate codes and a forward reference must not be dressed up as a cycle.
    a = base(steps=[Step(id="s1", action="fill", locator=loc("x"),
                         value={"from_step": "s2"}, risk="safe"),
                    Step(id="s2", action="read", locator=loc("y"), extract="text",
                         into="balance", risk="safe")])
    assert "FROM_STEP_CYCLE" not in codes(a)


def test_a_from_step_naming_no_step_at_all_is_an_error() -> None:
    a = base(steps=[Step(id="s1", action="fill", locator=loc("x"),
                         value={"from_step": "s9"}, risk="safe"),
                    Step(id="s2", action="read", locator=loc("y"), extract="text",
                         into="balance", risk="safe")])
    assert "FROM_STEP_UNKNOWN_STEP" in codes(a)


def test_duplicate_step_ids_are_an_error() -> None:
    # `from_step` and Task 3's overlays both address a step by id. Two steps sharing one id
    # makes both addressings ambiguous, and a dict keyed by id would silently keep one.
    a = base(steps=[Step(id="s1", action="click", locator=loc("x"), risk="safe"),
                    Step(id="s1", action="read", locator=loc("y"), extract="text",
                         into="balance", risk="safe")])
    assert "DUPLICATE_STEP_ID" in codes(a)


def test_a_literal_filling_a_sensitive_field_is_an_error() -> None:
    # §4.2 decision 8 and §8.3 step 4: the compiler refuses to persist a literal read from a
    # protected or sensitive field, and the validator refuses to load one that got through.
    a = base(steps=[Step(id="s1", action="fill", locator=loc("Password"),
                         value={"literal": "hunter2"}, risk="safe")])
    assert "LITERAL_FROM_PROTECTED_FIELD" in codes(a)


def test_a_protected_token_is_matched_on_word_boundaries_not_as_a_substring() -> None:
    # The same rule phase 2's parser uses, and for the same reason: a naive substring match
    # makes "Shipping" (pin) and "Spinner" (pin) protected fields.
    a = base(steps=[Step(id="s1", action="fill", locator=loc("Shipping"),
                         value={"literal": "12345"}, risk="safe"),
                    Step(id="s2", action="read", locator=loc("y"), extract="text",
                         into="balance", risk="safe")])
    assert "LITERAL_FROM_PROTECTED_FIELD" not in codes(a)


def test_a_literal_filling_a_field_named_by_a_sensitive_input_is_an_error() -> None:
    # The second signal available at load time: the control being filled corresponds, by
    # name, to an input the artifact itself declares `sensitive: true`.
    a = base(
        inputs={"member_id": InputSpec(type="string"),
                "security_answer": InputSpec(type="string", sensitive=True)},
        steps=[Step(id="s1", action="fill", locator=loc("Security Answer"),
                    value={"literal": "fido"}, risk="safe"),
               Step(id="s2", action="read", locator=loc("y"), extract="text",
                    into="balance", risk="safe")],
    )
    assert "LITERAL_FROM_PROTECTED_FIELD" in codes(a)


def test_a_literal_filling_an_ordinary_field_is_fine() -> None:
    a = base(steps=[Step(id="s1", action="fill", locator=loc("Member ID"),
                         value={"literal": "12345"}, risk="safe"),
                    Step(id="s2", action="read", locator=loc("y"), extract="text",
                         into="balance", risk="safe")])
    assert "LITERAL_FROM_PROTECTED_FIELD" not in codes(a)


def test_ordinal_usage_warns_but_does_not_error() -> None:
    # §3.4 rule 2 and §4.4: ordinal is the sanctioned last resort, and it is reviewable.
    a = base(steps=[Step(id="s1", action="fill", locator=loc("Member ID", ordinal=2),
                         value={"from_input": "member_id"}, risk="safe"),
                    Step(id="s2", action="read", locator=loc("y"), extract="text",
                         into="balance", risk="safe")])
    findings = validate(a)
    assert any(f.code == "ORDINAL_USED" and f.level == "warning" for f in findings)
    assert [f for f in findings if f.level == "error"] == []


def test_an_ordinal_hidden_in_a_scope_or_a_fallback_still_warns() -> None:
    scoped = loc("Savings")
    scoped.scope = loc("Accounts", ordinal=1)
    a = base(steps=[Step(id="s1", action="read", locator=scoped, extract="text",
                         into="balance", risk="safe")])
    assert any(f.code == "ORDINAL_USED" for f in validate(a))


def test_a_step_with_no_risk_classification_is_an_error() -> None:
    # §6.3's asymmetry, carried into load time: over-classification costs a human a moment
    # at approval, while an unclassified step later *treated* as safe is the error that
    # cannot be undone. A step nobody has classified must not be replayable or approvable.
    a = base(steps=[Step(id="s1", action="read", locator=loc("y"), extract="text",
                         into="balance")])
    assert any(f.code == "RISK_UNCLASSIFIED" and f.level == "error" for f in validate(a))


def test_without_a_deployment_the_narrowing_check_reports_that_it_was_skipped() -> None:
    # E4: a caller must never mistake "not checked" for "checked and clean".
    assert any(f.code == "ALLOWLIST_NOT_CHECKED" and f.level == "note" for f in validate(base()))


def test_the_skipped_note_distinguishes_no_policy_from_a_declared_one() -> None:
    # Task 1 made `policy` nullable precisely so this distinction survives parsing; the note
    # must carry it rather than erase it again.
    undeclared = [f for f in validate(base()) if f.code == "ALLOWLIST_NOT_CHECKED"]
    declared = [f for f in validate(base(policy=CapabilityPolicy(allowed_paths=["/teller/"])))
                if f.code == "ALLOWLIST_NOT_CHECKED"]
    assert len(undeclared) == len(declared) == 1
    assert undeclared[0].message != declared[0].message


DEPLOYMENT = DeploymentAllowlist(
    allowed_origins=["https://acme.corebank.example"],
    allowed_paths=["/teller/"],
    denied_paths=["/teller/admin/"],
    allowed_actions=["navigate", "click", "fill", "read"],
)


def test_with_a_deployment_supplied_the_skipped_note_is_not_emitted() -> None:
    assert "ALLOWLIST_NOT_CHECKED" not in {f.code for f in validate(base(), DEPLOYMENT)}


def test_a_policy_that_narrows_the_deployment_allowlist_is_clean() -> None:
    a = base(policy=CapabilityPolicy(
        allowed_origins=["https://acme.corebank.example"],
        allowed_paths=["/teller/search"],
        allowed_actions=["fill", "read"],
    ))
    assert [f for f in validate(a, DEPLOYMENT) if f.level == "error"] == []


def test_an_undeclared_policy_never_widens_anything() -> None:
    # `None` means "not narrowed", not "narrowed to nothing" -- inheriting the deployment's
    # own set is not a widening.
    assert [f for f in validate(base(), DEPLOYMENT) if f.level == "error"] == []


def test_a_policy_origin_outside_the_deployment_widens_and_is_an_error() -> None:
    a = base(policy=CapabilityPolicy(allowed_origins=["https://other.example"]))
    assert any(f.code == "POLICY_WIDENS_ALLOWLIST" and f.level == "error"
               for f in validate(a, DEPLOYMENT))


def test_a_policy_path_outside_the_deployment_widens_and_is_an_error() -> None:
    a = base(policy=CapabilityPolicy(allowed_paths=["/admin/console"]))
    assert "POLICY_WIDENS_ALLOWLIST" in {f.code for f in validate(a, DEPLOYMENT)}


def test_a_policy_path_inside_a_denied_prefix_widens_and_is_an_error() -> None:
    # §6.1: deny rules are evaluated first and win. A capability policy re-permitting a
    # denied path is the widening this check exists to catch.
    a = base(policy=CapabilityPolicy(allowed_paths=["/teller/admin/close"]))
    assert "POLICY_WIDENS_ALLOWLIST" in {f.code for f in validate(a, DEPLOYMENT)}


def test_a_policy_action_outside_the_deployment_widens_and_is_an_error() -> None:
    a = base(policy=CapabilityPolicy(allowed_actions=["fill", "select"]))
    assert "POLICY_WIDENS_ALLOWLIST" in {f.code for f in validate(a, DEPLOYMENT)}


def test_validation_reports_every_defect_rather_than_stopping_at_the_first() -> None:
    # A human reviewing an artifact wants the whole list, not the first thing that went
    # wrong, so `validate` never raises and never short-circuits.
    a = base(
        outputs={"balance": OutputSpec(type="string"), "orphan": OutputSpec(type="string")},
        steps=[Step(id="s1", action="fill", locator=loc("Password"),
                    value={"literal": "hunter2"})],
    )
    expected = {"LITERAL_FROM_PROTECTED_FIELD", "RISK_UNCLASSIFIED", "OUTPUT_NEVER_PRODUCED"}
    assert expected <= codes(a)


def test_the_base_fixture_matches_the_spec_s_own_success_checkpoint() -> None:
    # M8: S4.1's example success checkpoint is
    # `{ role: heading, name_match: contains, name: "Member " }`. Omitting `name_match`
    # silently defaults it to `exact`, which is a different predicate. Inert for validation,
    # but Tasks 3-5 inherit this fixture and a fixture that quietly disagrees with the spec
    # is how a wrong assumption propagates.
    assert base().success.checkpoint.name_match == "contains"


def test_a_locator_scope_cycle_does_not_make_validate_raise() -> None:
    # M3: `validate` documents that it never raises, and a cyclic scope chain is
    # constructible through ordinary Pydantic attribute assignment -- no `model_construct`
    # bypass needed. Not reachable from a YAML load, but Task 3 mutates locators in memory
    # while resolving an overlay, which is exactly where this would first be hit.
    #
    # E9 changed what it reports, not whether it terminates: the cycle is now an error
    # rather than a silent prune. Both edge kinds close a loop, so `scope` is checked too
    # even though the code is named for the fallback case.
    outer = loc("Savings")
    inner = loc("Accounts")
    outer.scope = inner
    inner.scope = outer
    a = base(steps=[Step(id="s1", action="read", locator=outer, extract="text",
                         into="balance", risk="safe")])
    assert [f.code for f in validate(a) if f.level == "error"] == ["LOCATOR_FALLBACK_CYCLE"]


def test_a_locator_fallback_cycle_is_an_error() -> None:
    # E9. Before this, an artifact carrying a cyclic fallback chain validated as fully
    # clean and the cycle was then pruned silently at resolution time (E8's guard). Silent
    # acceptance followed by silent pruning leaves no diagnostic trail anywhere, and phase
    # 3's compiler is the first real producer of `fallbacks` -- a compiler bug emitting one
    # would have had nothing to catch it.
    primary = loc("Select")
    alternate = loc("Choose")
    primary.fallbacks = [alternate]
    alternate.fallbacks = [primary]
    a = base(steps=[Step(id="s1", action="click", locator=primary, risk="safe"),
                    Step(id="s2", action="read", locator=loc("y"), extract="text",
                         into="balance", risk="safe")])
    assert any(f.code == "LOCATOR_FALLBACK_CYCLE" and f.level == "error"
               for f in validate(a))


def test_a_locator_diamond_is_not_reported_as_a_cycle() -> None:
    # The other direction, and the one this class of check gets wrong: a locator reachable
    # through two different branches is revisited, but it is not a loop. Distinguishing them
    # needs the current DFS path, not merely a visited set -- a check keyed on "have I seen
    # this before" would fire here and would then fire on everything.
    shared = loc("Shared")
    left = loc("Left")
    right = loc("Right")
    left.fallbacks = [shared]
    right.fallbacks = [shared]
    root = loc("Root")
    root.fallbacks = [left, right]
    a = base(steps=[Step(id="s1", action="click", locator=root, risk="safe"),
                    Step(id="s2", action="read", locator=loc("y"), extract="text",
                         into="balance", risk="safe")])
    assert "LOCATOR_FALLBACK_CYCLE" not in codes(a)


def test_a_scope_and_fallback_diamond_meeting_at_one_locator_is_not_a_cycle() -> None:
    # The mixed-edge diamond: `scope` and `fallbacks` reach the same locator. Still not a
    # loop, and the traversal crosses both edge kinds, so it is worth its own case.
    shared = loc("Shared")
    root = loc("Root")
    root.scope = shared
    root.fallbacks = [shared]
    a = base(steps=[Step(id="s1", action="click", locator=root, risk="safe"),
                    Step(id="s2", action="read", locator=loc("y"), extract="text",
                         into="balance", risk="safe")])
    assert "LOCATOR_FALLBACK_CYCLE" not in codes(a)


def test_a_locator_fallback_cycle_does_not_make_validate_raise() -> None:
    first = loc("Savings")
    second = loc("Password")
    first.fallbacks = [second]
    second.fallbacks = [first]
    a = base(steps=[Step(id="s1", action="fill", locator=first,
                         value={"literal": "hunter2"}, risk="safe"),
                    Step(id="s2", action="read", locator=loc("y"), extract="text",
                         into="balance", risk="safe")])
    # It must terminate *and* still see the protected name hiding in the cycle.
    assert "LITERAL_FROM_PROTECTED_FIELD" in codes(a)


def test_a_step_drawing_its_value_from_itself_is_a_cycle_not_a_forward_reference() -> None:
    # M5: a self-reference is a cycle -- the step can never produce a value for itself --
    # and it is not a forward reference, because there is no reordering that fixes it.
    a = base(steps=[Step(id="s1", action="fill", locator=loc("x"),
                         value={"from_step": "s1"}, risk="safe"),
                    Step(id="s2", action="read", locator=loc("y"), extract="text",
                         into="balance", risk="safe")])
    found = codes(a)
    assert "FROM_STEP_CYCLE" in found
    assert "FROM_STEP_FORWARD_REFERENCE" not in found


def test_a_policy_path_enclosing_a_denied_subtree_is_not_reported() -> None:
    # M6: deny precedence is checked in one direction only. `/teller/admin/close` sits
    # inside the denied `/teller/admin/` and is caught; `/teller/`, which *encloses* that
    # denied subtree, is not. Pinned so the asymmetry is a decision and not an accident --
    # see `_narrowing_findings` for why it is the right one.
    a = base(policy=CapabilityPolicy(allowed_paths=["/teller/"]))
    assert [f for f in validate(a, DEPLOYMENT) if f.level == "error"] == []
