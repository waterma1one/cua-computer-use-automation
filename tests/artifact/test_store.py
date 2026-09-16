"""The store: where the immutable artifact file and the mutable registry part company.

Spec §4.1's closing paragraph: lifecycle state (`status`, `stability`) is deliberately not
in the artifact file. It lives in `artifacts/registry.json`, keyed by `(id, version)`. The
artifact file at `artifacts/<id>/v<version>.yaml` is immutable -- a new revision is a new
version number, never an overwrite.

This module is the only one in `cua/artifact/` that touches the filesystem. Everything
else in the package is pure data shaping (`models.py`, `validate.py`, `overlay.py`,
`schema.py`); this is where that data actually gets written and read back.

Two acceptance criteria a reviewer checks first are both pinned here:

1. The serialized artifact contains no hostname, credential, CSS selector, XPath, or
   regular expression (with the one declared exception, `InputSpec.pattern`). Enforced
   twice: once as a fixture-shaped scan against the bytes on disk (this module, unchanged
   from the original brief), and once as the `FORBIDDEN_CONTENT` / `LITERAL_*` finding
   family in `cua.artifact.validate` (rulings E16 and E22), which `save`, `load` and the
   approval gate all inherit, so it runs on whatever the store is actually given -- through
   any door -- not only on what a fixture happens to carry.
2. `status` and `stability` appear only in `registry.json`, never in the artifact file.
"""

import json
import logging
import os

import pytest
import yaml
from pydantic import ValidationError

from cua.artifact.models import Expect, InputSpec, LiteralValue, Matcher, OutputSpec, Step
from cua.artifact.store import RegistryEntry, load, read_registry, save, write_registry_entry
from cua.surface.models import Locator
from tests.artifact.factories import PATH, base, loc


def test_a_saved_artifact_round_trips(tmp_path) -> None:
    path = save(base(), tmp_path)
    artifact, _findings = load("corebank.probe", 1, tmp_path)
    assert artifact == base()
    assert path.name == "v1.yaml"


def test_the_serialized_artifact_contains_no_hostname_selector_xpath_or_credential(
    tmp_path,
) -> None:
    # Acceptance criterion 1, asserted against the bytes on disk rather than against the model,
    # because the file is the reviewable deliverable. This scan is deliberately blunt and will
    # over-trigger by design -- it is meant to catch a forbidden value arriving through any
    # field, including one added by a phase not yet written. If a legitimate value ever trips
    # it, the fix is to narrow that one value, never to soften the scan.
    text = save(base(), tmp_path).read_text().lower()
    for forbidden in ("http://", "https://", "localhost", "127.0.0.1", ".com", ".example",
                      "//div", "//*[", "css=", "xpath", "querySelector".lower(), "nth-child",
                      "password", "secret"):
        assert forbidden not in text, f"{forbidden!r} reached the artifact file"


def test_an_input_pattern_survives_serialization(tmp_path) -> None:
    # The one deliberate exception to "no regular expression": inputs[].pattern is a JSON Schema
    # validation pattern for a caller's argument, not a locator. It must not be scrubbed.
    from cua.artifact.models import InputSpec
    a = base(inputs={"member_id": InputSpec(type="string", pattern="^[0-9]{5}$", required=True)})
    assert "^[0-9]{5}$" in save(a, tmp_path).read_text()


def test_status_and_stability_never_reach_the_artifact_file(tmp_path) -> None:
    # Acceptance criterion 2, and §4.1: the artifact file is immutable, lifecycle state is not.
    #
    # Restored to the brief's original order -- register, THEN save (fix round 2, item 2). Fix
    # round 1 reordered this to save-then-register to satisfy ruling E18's ghost-id refusal,
    # but a re-review mutant (a `save` that folds any *existing* registry entry's status/
    # stability into the artifact file) passed cleanly against that reordering: the registry was
    # still empty at save time, so there was nothing yet to leak, and the test proved nothing.
    # `artifact=a` makes the original order legal again under E18 (path 1: the passed artifact's
    # identity matches the key, and it holds no unverified expects and passes load-time
    # validation, so approving it needs no file on disk at all).
    a = base()
    write_registry_entry(
        tmp_path, "corebank.probe", 1,
        RegistryEntry(status="approved", replays=3, successes=3), artifact=a,
    )
    text = save(a, tmp_path).read_text().lower()
    assert "status" not in text
    assert "stability" not in text
    assert "approved" not in text


def test_the_registry_holds_lifecycle_state_keyed_by_id_and_version(tmp_path) -> None:
    # Ruling E18: *approving* an (id, version) the store holds no file for is refused (the
    # ghost-id hole), so both versions are saved to disk first. A `draft` entry needs no file
    # -- `test_write_registry_entry_draft_with_no_artifact_and_no_file_still_writes` pins
    # that -- and it is only the `approved` write below that would be refused without one.
    save(base(), tmp_path)
    save(base(version=2), tmp_path)
    write_registry_entry(tmp_path, "corebank.probe", 1, RegistryEntry(status="draft"))
    write_registry_entry(tmp_path, "corebank.probe", 2, RegistryEntry(status="approved"))
    registry = read_registry(tmp_path)
    assert registry["corebank.probe"]["1"].status == "draft"
    assert registry["corebank.probe"]["2"].status == "approved"
    assert json.loads((tmp_path / "artifacts" / "registry.json").read_text())


def test_saving_over_an_existing_version_is_refused(tmp_path) -> None:
    # §4.1: artifacts/<id>/v3.yaml is immutable. A new revision is a new version number.
    save(base(), tmp_path)
    with pytest.raises(FileExistsError):
        save(base(), tmp_path)


def test_an_artifact_holding_an_unverified_expect_cannot_be_marked_approved(tmp_path) -> None:
    # Acceptance criterion 6, and §8.3 step 5: the compiler cannot invent knowledge of states it
    # never saw, so an artifact that admits it has unproven branches does not get promoted.
    a = base()
    a.steps[0].expects = [
        Expect(when=Matcher(role="heading", name="x"), outcome="continue", source="proposed")
    ]
    save(a, tmp_path)
    with pytest.raises(ValueError):
        write_registry_entry(tmp_path, "corebank.probe", 1,
                             RegistryEntry(status="approved"), artifact=a)


# --- Coverage beyond the brief's pinned tests: both directions on the checks above, and the
# behaviors the brief's step 2 describes in prose but does not spell out in test code. ---


def test_an_artifact_with_no_expects_at_all_can_be_approved(tmp_path) -> None:
    # The opposite of the unverified-expect test above: `base()` declares no `expects` on any
    # step, so there is nothing unverified to object to, and approval must go through with the
    # artifact supplied for inspection.
    a = base()
    save(a, tmp_path)
    write_registry_entry(
        tmp_path, "corebank.probe", 1, RegistryEntry(status="approved"), artifact=a,
    )
    registry = read_registry(tmp_path)
    assert registry["corebank.probe"]["1"].status == "approved"


def test_an_explicitly_verified_expect_does_not_block_approval(tmp_path) -> None:
    # Criterion 6 reads "any unverified expect", not "any proposed expect" -- the two are not
    # the same thing, because `source="observed"` may later be marked `verified=True` by
    # self-verification (only `source="proposed"` is permanently barred from `verified=True`
    # by the model validator on `Expect`). An observed-and-verified clause must not block
    # approval, which distinguishes this gate from one that fires on source alone.
    a = base()
    a.steps[0].expects = [
        Expect(when=Matcher(role="heading", name="x"), outcome="continue",
               source="observed", verified=True)
    ]
    save(a, tmp_path)
    write_registry_entry(
        tmp_path, "corebank.probe", 1, RegistryEntry(status="approved"), artifact=a,
    )
    registry = read_registry(tmp_path)
    assert registry["corebank.probe"]["1"].status == "approved"


def test_an_observed_but_unverified_expect_also_blocks_approval(tmp_path) -> None:
    # The other half of the same distinction: an `observed` clause that has *not* been marked
    # verified is still unverified, and criterion 6 says "any unverified expect" -- not "any
    # proposed expect" -- so this must be blocked exactly like the proposed case is.
    a = base()
    a.steps[0].expects = [
        Expect(when=Matcher(role="heading", name="x"), outcome="continue", source="observed")
    ]
    save(a, tmp_path)
    with pytest.raises(ValueError):
        write_registry_entry(tmp_path, "corebank.probe", 1,
                             RegistryEntry(status="approved"), artifact=a)


def test_an_unverified_expect_on_a_later_step_blocks_approval(tmp_path) -> None:
    # I6: every prior gate test put its lone expect on steps[0]; a traversal accidentally
    # narrowed to steps[:1] would pass all of them. base() has a second step (s2); put the
    # unverified expect there instead.
    a = base()
    a.steps[1].expects = [
        Expect(when=Matcher(role="heading", name="x"), outcome="continue", source="proposed")
    ]
    save(a, tmp_path)
    with pytest.raises(ValueError):
        write_registry_entry(tmp_path, "corebank.probe", 1,
                             RegistryEntry(status="approved"), artifact=a)


def test_an_unverified_expect_as_a_later_expect_on_the_same_step_blocks_approval(tmp_path) -> None:
    # I6's other half: every prior gate test gave its step exactly one expect; a traversal
    # accidentally narrowed to expects[:1] would pass this if the unverified one is second.
    a = base()
    a.steps[0].expects = [
        Expect(when=Matcher(role="heading", name="y"), outcome="continue",
               source="observed", verified=True),
        Expect(when=Matcher(role="heading", name="x"), outcome="continue", source="proposed"),
    ]
    save(a, tmp_path)
    with pytest.raises(ValueError):
        write_registry_entry(tmp_path, "corebank.probe", 1,
                             RegistryEntry(status="approved"), artifact=a)


def test_an_artifact_with_an_unverified_expect_can_still_be_left_draft(tmp_path) -> None:
    # The gate is specific to `status="approved"`; an unverified artifact must still be
    # storable as a draft. Otherwise a fresh discovery run could never be registered at all.
    a = base()
    a.steps[0].expects = [
        Expect(when=Matcher(role="heading", name="x"), outcome="continue", source="proposed")
    ]
    save(a, tmp_path)
    write_registry_entry(tmp_path, "corebank.probe", 1, RegistryEntry(status="draft"), artifact=a)
    registry = read_registry(tmp_path)
    assert registry["corebank.probe"]["1"].status == "draft"


def test_write_registry_entry_refuses_when_artifact_identity_does_not_match_the_key(
    tmp_path,
) -> None:
    # I1 / ruling E18: an artifact's own (id, version) must match the key it is being
    # registered under, or this raises rather than silently gating on the wrong capability.
    a = base()  # id="corebank.probe", version=1
    with pytest.raises(ValueError):
        write_registry_entry(tmp_path, "corebank.probe", 2,
                             RegistryEntry(status="approved"), artifact=a)
    with pytest.raises(ValueError):
        write_registry_entry(tmp_path, "someone.else", 1,
                             RegistryEntry(status="approved"), artifact=a)


def test_write_registry_entry_refuses_approving_a_ghost_id_with_no_artifact_and_no_file(
    tmp_path,
) -> None:
    # I1 / ruling E18: no artifact supplied and no file on disk for this (id, version) means
    # there is nothing to check -- approving would register a capability the store never
    # held. This is the "ghost.id" hole named in the finding.
    with pytest.raises(ValueError):
        write_registry_entry(tmp_path, "ghost.id", 7, RegistryEntry(status="approved"))


def test_write_registry_entry_draft_with_no_artifact_and_no_file_still_writes(tmp_path) -> None:
    # The other half of the ghost-id rule: `draft` never needed an artifact to check against
    # before, and still does not. A fresh discovery run must be registrable before it has been
    # saved to disk or handed back to this call.
    write_registry_entry(tmp_path, "corebank.probe", 1, RegistryEntry(status="draft"))
    registry = read_registry(tmp_path)
    assert registry["corebank.probe"]["1"].status == "draft"


def test_write_registry_entry_refuses_clearing_requires_human_approval_with_nothing_resolved(
    tmp_path,
) -> None:
    # E15's other clause (fix round 2, item 1): the fallback for requires_human_approval is
    # True, not "silently rewrite whatever the caller asked for to True". With no artifact and
    # no file to check an irreversible step against, the store cannot prove the flag clearable,
    # so an explicit False is refused rather than silently accepted or silently overwritten.
    with pytest.raises(ValueError):
        write_registry_entry(tmp_path, "ghost.id", 8, RegistryEntry(requires_human_approval=False))


def test_write_registry_entry_default_requires_human_approval_writes_with_nothing_resolved(
    tmp_path,
) -> None:
    # The other direction: the field's own default (True) needs no artifact to justify itself,
    # so a draft entry that never touches the field still writes with nothing resolved.
    write_registry_entry(tmp_path, "ghost.id", 8, RegistryEntry(status="draft"))
    registry = read_registry(tmp_path)
    assert registry["ghost.id"]["8"].requires_human_approval is True


# --- E19: a draft registration never depends on load-time validation passing; approval
# always does -- pinned identically for the on-disk-file path and the passed-artifact path. ---


def test_e19_draft_registers_a_file_that_fails_load_time_validation(tmp_path) -> None:
    a = base()
    a.steps[0].risk = None  # RISK_UNCLASSIFIED -- an error-level validate() finding
    save(a, tmp_path)
    write_registry_entry(tmp_path, "corebank.probe", 1, RegistryEntry(status="draft"))
    registry = read_registry(tmp_path)
    assert registry["corebank.probe"]["1"].status == "draft"


def test_e19_approval_of_a_file_that_fails_load_time_validation_is_refused(tmp_path) -> None:
    a = base()
    a.steps[0].risk = None
    save(a, tmp_path)
    with pytest.raises(ValueError):
        write_registry_entry(tmp_path, "corebank.probe", 1, RegistryEntry(status="approved"))


def test_e19_draft_registers_a_passed_artifact_that_fails_load_time_validation(tmp_path) -> None:
    # The passed-artifact path must behave identically to the on-disk path above -- the
    # asymmetry ruling E19 closes is between the two paths, not just within one of them.
    a = base()
    a.steps[0].risk = None
    write_registry_entry(tmp_path, "corebank.probe", 1, RegistryEntry(status="draft"), artifact=a)
    registry = read_registry(tmp_path)
    assert registry["corebank.probe"]["1"].status == "draft"


def test_e19_approval_of_a_passed_artifact_that_fails_load_time_validation_is_refused(
    tmp_path,
) -> None:
    a = base()
    a.steps[0].risk = None
    with pytest.raises(ValueError):
        write_registry_entry(
            tmp_path, "corebank.probe", 1, RegistryEntry(status="approved"), artifact=a,
        )


def test_write_registry_entry_with_no_artifact_argument_gates_on_the_file_when_clean(
    tmp_path,
) -> None:
    # Ruling E18's second resolution path: no `artifact` argument, but a file exists on disk
    # for this (id, version) -- it is loaded and gates on its actual contents. Clean direction.
    save(base(), tmp_path)
    write_registry_entry(tmp_path, "corebank.probe", 1, RegistryEntry(status="approved"))
    registry = read_registry(tmp_path)
    assert registry["corebank.probe"]["1"].status == "approved"


def test_write_registry_entry_with_no_artifact_argument_gates_on_the_file_when_unverified(
    tmp_path,
) -> None:
    # The other direction of the same path: the file on disk holds an unverified expect, so
    # approval is refused even though no `artifact` argument was supplied to this call.
    a = base()
    a.steps[0].expects = [
        Expect(when=Matcher(role="heading", name="x"), outcome="continue", source="proposed")
    ]
    save(a, tmp_path)
    with pytest.raises(ValueError):
        write_registry_entry(tmp_path, "corebank.probe", 1, RegistryEntry(status="approved"))


def test_marking_approved_with_no_artifact_supplied_is_not_gated(tmp_path) -> None:
    # `artifact` is optional on `write_registry_entry` -- e.g. an operator flipping a registry
    # status from the console without recompiling. With no artifact argument AND a file already
    # on disk, the previous version of this test's name ("is not gated") stops being accurate
    # under ruling E18: the store now resolves the file and gates on its real contents. What
    # *is* still true, and is what this test now pins, is that supplying no `artifact` argument
    # is not itself a way to bypass the gate -- the file's own contents are still checked.
    save(base(), tmp_path)
    write_registry_entry(tmp_path, "corebank.probe", 1, RegistryEntry(status="approved"))
    registry = read_registry(tmp_path)
    assert registry["corebank.probe"]["1"].status == "approved"


def test_reading_a_registry_that_does_not_exist_yet_is_empty(tmp_path) -> None:
    # No writes have happened; there is no registry.json on disk at all. This must not raise.
    assert read_registry(tmp_path) == {}


def test_saving_a_second_version_of_the_same_id_is_allowed(tmp_path) -> None:
    # The immutability check is per-version, not per-id: a new version number is exactly how
    # a revision is supposed to be recorded.
    save(base(), tmp_path)
    path = save(base(version=2), tmp_path)
    assert path.name == "v2.yaml"
    assert load("corebank.probe", 1, tmp_path)[0].version == 1
    assert load("corebank.probe", 2, tmp_path)[0].version == 2


def test_load_surfaces_findings_rather_than_dropping_them(tmp_path) -> None:
    # Task 2's handoff: "validate is the load-time gate -- call it on read, and surface
    # warnings and notes to the operator rather than dropping them." A clean `base()` artifact
    # still carries the E4 ALLOWLIST_NOT_CHECKED note (no DeploymentAllowlist is available to
    # this store), so a plain `load()` call -- the default shape, not an opt-in (ruling E17) --
    # must expose it rather than silently discarding it.
    save(base(), tmp_path)
    artifact, findings = load("corebank.probe", 1, tmp_path)
    assert artifact == base()
    assert any(f.code == "ALLOWLIST_NOT_CHECKED" for f in findings)
    assert not any(f.level == "error" for f in findings)


def test_load_raises_on_an_artifact_with_an_error_level_finding(tmp_path) -> None:
    # The other half of "load() validates and surfaces": an error-level finding must not be
    # silently returned as though the artifact were replayable. Round-tripping an artifact
    # with an unclassified step's risk (RISK_UNCLASSIFIED, an error) through the store and
    # loading it back must raise rather than hand back a broken artifact as if it were fine.
    a = base()
    a.steps[0].risk = None
    save(a, tmp_path)
    with pytest.raises(ValueError):
        load("corebank.probe", 1, tmp_path)


def test_load_of_a_nonexistent_version_raises_file_not_found(tmp_path) -> None:
    save(base(), tmp_path)
    with pytest.raises(FileNotFoundError):
        load("corebank.probe", 99, tmp_path)


def test_save_writes_yaml_with_declared_key_order_not_alphabetical(tmp_path) -> None:
    # §4.1: "Write YAML with sort_keys=False so the file reads in the order §4.1 declares."
    # `schema_version` is declared before `id`, which alphabetical order would reverse.
    path = save(base(), tmp_path)
    lines = [line for line in path.read_text().splitlines() if line and not line.startswith(" ")]
    keys = [line.split(":", 1)[0] for line in lines]
    assert keys.index("schema_version") < keys.index("id")


def test_write_registry_entry_persists_stability_fields(tmp_path) -> None:
    write_registry_entry(
        tmp_path, "corebank.probe", 1,
        RegistryEntry(status="draft", replays=10, successes=7, score=0.7),
    )
    registry = read_registry(tmp_path)
    entry = registry["corebank.probe"]["1"]
    assert entry.replays == 10
    assert entry.successes == 7
    assert entry.score == 0.7


def test_registry_entry_rejects_unknown_fields() -> None:
    # E5's `extra="forbid"` convention, carried into the one model this task adds.
    with pytest.raises(ValueError):
        RegistryEntry(status="draft", stability="bogus")  # type: ignore[call-arg]


# --- I5: the registry's two most consequential defaults, pinned in both directions -------


def test_registry_entry_status_defaults_to_draft() -> None:
    assert RegistryEntry().status == "draft"


def test_registry_entry_requires_human_approval_defaults_to_true() -> None:
    assert RegistryEntry().requires_human_approval is True


# --- E15: requires_human_approval is un-clearable on an irreversible artifact -------------


def test_write_registry_entry_refuses_clearing_requires_human_approval_on_an_irreversible_step(
    tmp_path,
) -> None:
    a = base()
    a.steps[0].risk = "irreversible"
    save(a, tmp_path)
    with pytest.raises(ValueError):
        write_registry_entry(
            tmp_path, "corebank.probe", 1,
            RegistryEntry(status="draft", requires_human_approval=False), artifact=a,
        )


def test_write_registry_entry_allows_clearing_requires_human_approval_on_a_safe_artifact(
    tmp_path,
) -> None:
    a = base()  # every step is risk="safe"
    save(a, tmp_path)
    write_registry_entry(
        tmp_path, "corebank.probe", 1,
        RegistryEntry(status="draft", requires_human_approval=False), artifact=a,
    )
    registry = read_registry(tmp_path)
    assert registry["corebank.probe"]["1"].requires_human_approval is False


def test_the_irreversible_refusal_walks_every_step_not_just_the_first(tmp_path) -> None:
    a = base()
    a.steps[1].risk = "irreversible"  # not steps[0]
    save(a, tmp_path)
    with pytest.raises(ValueError):
        write_registry_entry(
            tmp_path, "corebank.probe", 1,
            RegistryEntry(status="draft", requires_human_approval=False), artifact=a,
        )


# --- E16: criterion 1 enforced at save(), pinned against every injection the reviewer used,
# and against the legitimate values the check must not reproduce M1's false positives on. ---


def test_save_refuses_a_bare_hostname_in_description(tmp_path) -> None:
    a = base(description="Uses acme.corebank.internal as the login host.")
    with pytest.raises(ValueError):
        save(a, tmp_path)


def test_save_refuses_an_ip_address_with_a_port(tmp_path) -> None:
    a = base(description="The health check hits 10.0.0.5:8080 directly.")
    with pytest.raises(ValueError):
        save(a, tmp_path)


def test_save_refuses_a_css_selector_in_locator_rationale(tmp_path) -> None:
    a = base()
    a.steps[0].locator = Locator(
        role="button", name="Member ID", surface_path=PATH, rationale=".btn-primary",
        confidence="high",
    )
    with pytest.raises(ValueError):
        save(a, tmp_path)


def test_save_refuses_an_xpath_in_locator_name(tmp_path) -> None:
    a = base()
    a.steps[0].locator = Locator(
        role="button", name="//button[@id='x']", surface_path=PATH, rationale="test fixture",
        confidence="high",
    )
    with pytest.raises(ValueError):
        save(a, tmp_path)


def test_save_refuses_a_credential_literal_into_a_protected_locator(tmp_path) -> None:
    a = base()
    a.steps[0].locator = Locator(
        role="textbox", name="Password", surface_path=PATH, rationale="test fixture",
        confidence="high",
    )
    a.steps[0].value = LiteralValue(literal="hunter2")
    with pytest.raises(ValueError):
        save(a, tmp_path)


def test_save_allows_a_password_named_field_filled_from_an_input_not_a_literal(tmp_path) -> None:
    # M1's blind spot, deliberately not reproduced: a control genuinely named "Password" is
    # legitimate as long as nothing writes a literal into it. base()'s step already draws its
    # value from `from_input`, so only the locator's name changes here.
    a = base()
    a.steps[0].locator = Locator(
        role="textbox", name="Password", surface_path=PATH, rationale="test fixture",
        confidence="high",
    )
    path = save(a, tmp_path)
    assert path.exists()


def test_save_allows_an_output_named_account_status(tmp_path) -> None:
    # M1's other blind spot: an output name is a dict key, not a scanned value, and carries no
    # forbidden token regardless.
    a = base()
    a.outputs = {"account_status": OutputSpec(type="string")}
    a.steps[1].into = "account_status"
    path = save(a, tmp_path)
    assert path.exists()


# --- Final fix wave: the seams between the validator and the store ----------------------------
# Every construction below is the whole-phase reviewer's, executed rather than read.


def test_save_refuses_a_credential_literal_on_a_step_with_no_locator(tmp_path) -> None:
    # C1 / E20: the reviewer's exact construction. With no locator there was nothing for the
    # credential check to look at, and it returned the same empty list it returns for
    # "checked, found nothing", so this saved verbatim.
    a = base(steps=[Step(id="s1", action="press_key", value={"literal": "hunter2-real-password"},
                         risk="safe"),
                    Step(id="s2", action="read", locator=loc("y"), extract="text",
                         into="balance", risk="safe")])
    with pytest.raises(ValueError, match="LITERAL_WITHOUT_LOCATOR"):
        save(a, tmp_path)
    assert not (tmp_path / "artifacts").exists()


def test_save_accepts_a_press_key_with_a_locator_and_a_key_chord(tmp_path) -> None:
    a = base(steps=[Step(id="s1", action="press_key", locator=loc("Member ID"),
                         value={"literal": "Enter"}, risk="safe"),
                    Step(id="s2", action="read", locator=loc("y"), extract="text",
                         into="balance", risk="safe")])
    assert save(a, tmp_path).exists()


def test_a_hostname_shaped_input_key_never_reaches_save(tmp_path) -> None:
    # C2 / E21: the reviewer's exact construction. It is refused at construction by the
    # model's key constraint -- a `ValidationError`, before `save` is ever reached -- which
    # is what makes "a key is an identifier the schema itself constrains" a true statement.
    with pytest.raises(ValidationError):
        base(inputs={"http://acme.corebank.internal/evil": InputSpec(type="string", required=True)})


def _hand_edit(root, old: str, new: str) -> None:
    path = root / "artifacts" / "corebank.probe" / "v1.yaml"
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


def test_load_refuses_a_file_hand_edited_to_carry_a_hostname(tmp_path) -> None:
    # C3 / E22: criterion 1 is a property of the artifact, not of "an artifact that happened
    # to be written through save()". The reviewer saved a clean artifact, edited the bytes on
    # disk, and `load` handed it back with only the allowlist note.
    save(base(), tmp_path)
    _hand_edit(tmp_path, "rationale: test fixture", "rationale: see acme.corebank.internal")
    with pytest.raises(ValueError, match="FORBIDDEN_CONTENT"):
        load("corebank.probe", 1, tmp_path)


def test_approval_by_id_refuses_a_file_hand_edited_to_carry_a_hostname(tmp_path) -> None:
    # The same hand-edited file, through the approval gate's disk-resolution branch with no
    # `artifact` argument -- the path C3 showed could then approve it.
    save(base(), tmp_path)
    _hand_edit(tmp_path, "rationale: test fixture", "rationale: see acme.corebank.internal")
    with pytest.raises(ValueError, match="FORBIDDEN_CONTENT"):
        write_registry_entry(tmp_path, "corebank.probe", 1, RegistryEntry(status="approved"))


def test_save_still_accepts_an_artifact_with_a_non_criterion_1_error(tmp_path) -> None:
    # E22's scope: `save` refuses on the criterion-1 family only. E19 deliberately lets a
    # draft with `RISK_UNCLASSIFIED` be saved and registered; `load` and approval still
    # refuse it. `test_e19_draft_registers_a_file_that_fails_load_time_validation` pins the
    # registration half; this pins the save half by name.
    a = base()
    a.steps[0].risk = None
    assert save(a, tmp_path).exists()


def _spoof(root) -> None:
    # C4's construction: the bytes of corebank.probe/v1.yaml copied to another key's path.
    save(base(), root)
    source = root / "artifacts" / "corebank.probe" / "v1.yaml"
    target = root / "artifacts" / "spoofed.capability" / "v9.yaml"
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())


def test_load_refuses_a_file_whose_content_disagrees_with_its_path(tmp_path) -> None:
    # C4 / E23: `load("spoofed.capability", 9)` returned an artifact whose `.id` was
    # `corebank.probe`. Identity is checked wherever a file is resolved by key.
    _spoof(tmp_path)
    with pytest.raises(ValueError, match="spoofed.capability"):
        load("spoofed.capability", 9, tmp_path)


def test_approval_by_id_refuses_a_file_whose_content_disagrees_with_its_path(tmp_path) -> None:
    # The disk-resolution branch of the approval gate, with no `artifact` argument: E18's
    # identity check covered the passed-artifact path only, so this succeeded.
    _spoof(tmp_path)
    with pytest.raises(ValueError, match="spoofed.capability"):
        write_registry_entry(tmp_path, "spoofed.capability", 9, RegistryEntry(status="approved"))
    assert "spoofed.capability" not in read_registry(tmp_path)


def test_a_draft_registration_by_id_also_refuses_a_spoofed_file(tmp_path) -> None:
    # Same `ValueError` the passed-artifact path raises, regardless of status: a file whose
    # content disagrees with its path is a defect in every reading, not only at approval.
    _spoof(tmp_path)
    with pytest.raises(ValueError, match="spoofed.capability"):
        write_registry_entry(tmp_path, "spoofed.capability", 9, RegistryEntry(status="draft"))


def test_load_refuses_a_file_whose_version_disagrees_with_its_path(tmp_path) -> None:
    # The version half of the key, on its own: same id, wrong version number in the file.
    save(base(), tmp_path)
    _hand_edit(tmp_path, "version: 1\n", "version: 2\n")
    with pytest.raises(ValueError, match="v1"):
        load("corebank.probe", 1, tmp_path)


def test_a_matching_file_still_loads_after_the_identity_check(tmp_path) -> None:
    save(base(version=2), tmp_path)
    artifact, _ = load("corebank.probe", 2, tmp_path)
    assert (artifact.id, artifact.version) == ("corebank.probe", 2)


# --- E24: M3, M4, M5 closed rather than deferred a second time --------------------------------


def _artifact_dir(root):
    return root / "artifacts" / "corebank.probe"


def test_a_crash_before_the_rename_leaves_no_version_file_and_the_next_save_succeeds(
    tmp_path, monkeypatch,
) -> None:
    # M4: `save` used a bare `write_text`, so a crash mid-write left a truncated `v1.yaml`
    # the immutability check then refused to overwrite forever. The write now goes to a
    # sibling temp file that is renamed into place, so a crash leaves nothing at the final
    # path -- and nothing else in the directory either.
    def boom(src, dst):
        raise OSError("simulated crash during rename")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="simulated crash"):
        save(base(), tmp_path)
    assert not (_artifact_dir(tmp_path) / "v1.yaml").exists()
    assert list(_artifact_dir(tmp_path).iterdir()) == []

    monkeypatch.undo()
    assert save(base(), tmp_path).name == "v1.yaml"


def test_a_crash_while_writing_the_temp_file_leaves_no_partial_file_behind(
    tmp_path, monkeypatch,
) -> None:
    # M4, the other failure point: the temp file has been created but the write into it
    # fails. The half-written temp file must be removed, not left as litter beside the
    # artifact a reviewer opens.
    real_fdopen = os.fdopen

    class Broken:
        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()
            return False

        def write(self, text):
            raise OSError("simulated crash during write")

    monkeypatch.setattr(os, "fdopen", lambda fd, *a, **k: Broken(real_fdopen(fd, *a, **k)))
    with pytest.raises(OSError, match="simulated crash"):
        save(base(), tmp_path)
    assert list(_artifact_dir(tmp_path).iterdir()) == []

    monkeypatch.undo()
    assert save(base(), tmp_path).name == "v1.yaml"


def test_a_fresh_artifact_file_carries_no_null_valued_keys(tmp_path) -> None:
    # M5: `exclude_none=False` littered the file with `pattern: null`, `target: null`,
    # `scope: null`, `extract: null`, `policy: null` -- none of which §4.1's worked example
    # writes. The file is the reviewable deliverable and should read like the spec.
    text = save(base(), tmp_path).read_text()
    assert ": null" not in text
    assert "policy:" not in text
    assert "pattern:" not in text


def test_an_omitted_optional_parses_back_to_none(tmp_path) -> None:
    # M5's round-trip half: leaving a `None` out of the file must read back as `None`, not
    # as a parse failure or a different default. `test_a_saved_artifact_round_trips` proves
    # equality; this says which fields were omitted and what they came back as.
    save(base(), tmp_path)
    artifact, _ = load("corebank.probe", 1, tmp_path)
    assert artifact.policy is None
    assert artifact.inputs["member_id"].pattern is None
    assert artifact.steps[0].target is None
    assert artifact.steps[0].locator is not None and artifact.steps[0].locator.scope is None


def test_a_registry_entry_with_an_unknown_key_is_refused_rather_than_rewritten(
    tmp_path,
) -> None:
    # M3: `write_registry_entry` rewrote the raw JSON with `json.dumps`, re-serialising
    # entries `read_registry` would refuse. Now every entry passes through `RegistryEntry`
    # on the way back out, so the registry can never be written in a shape it cannot read.
    registry = tmp_path / "artifacts" / "registry.json"
    registry.parent.mkdir(parents=True)
    before = json.dumps({"other.capability": {"3": {"status": "draft", "stability": "bogus"}}})
    registry.write_text(before)
    with pytest.raises(ValueError):
        write_registry_entry(tmp_path, "corebank.probe", 1, RegistryEntry(status="draft"))
    assert registry.read_text() == before


def test_untouched_registry_entries_survive_a_write_through_the_model(tmp_path) -> None:
    # The direction M3's fix must not break: an existing, valid entry for another key is
    # carried through unchanged when a different key is written.
    write_registry_entry(tmp_path, "other.capability", 3,
                         RegistryEntry(status="draft", replays=4, successes=2, score=0.5))
    write_registry_entry(tmp_path, "corebank.probe", 1, RegistryEntry(status="draft"))
    registry = read_registry(tmp_path)
    assert registry["other.capability"]["3"] == RegistryEntry(
        status="draft", replays=4, successes=2, score=0.5,
    )
    assert registry["corebank.probe"]["1"].status == "draft"


# --- Deferred minor 8: approval by id logs what the gate saw ------------------------------------


def test_approval_by_id_logs_the_findings_the_gate_saw(tmp_path, caplog) -> None:
    # Approval by id stopped routing through `load` under E19, and with it lost E17's
    # logging: a warning-level finding seen during an on-disk approval was logged nowhere.
    # A clean `base()` still carries the ALLOWLIST_NOT_CHECKED note, so it must appear.
    save(base(), tmp_path)
    with caplog.at_level(logging.WARNING, logger="cua.artifact.store"):
        write_registry_entry(tmp_path, "corebank.probe", 1, RegistryEntry(status="approved"))
    assert any("ALLOWLIST_NOT_CHECKED" in record.getMessage() for record in caplog.records)


def test_a_fail_clause_with_no_code_saves_but_refuses_to_load(tmp_path) -> None:
    # The second step produces the declared `balance` output so OUTPUT_NEVER_PRODUCED
    # cannot also fire -- FAIL_CODE_NOT_A_FAILURE_KIND must be the only error-level
    # finding, or this test would pass even with that check disabled.
    a = base(steps=[Step(id="s1", action="click", locator=loc("x"), risk="safe",
                         expects=[Expect(when=Matcher(role="heading", name="x"),
                                        outcome="fail", source="observed")]),
                    Step(id="s2", action="read", locator=loc("y"), extract="text",
                         into="balance", risk="safe")])
    save(a, tmp_path)  # FAIL_CODE_NOT_A_FAILURE_KIND is not in CRITERION_1_CODES
    with pytest.raises(ValueError, match="FAIL_CODE_NOT_A_FAILURE_KIND"):
        load(a.id, a.version, tmp_path)


def test_load_refuses_a_syntactically_malformed_file_as_a_clean_value_error(tmp_path) -> None:
    # Final review fix wave: `yaml.YAMLError` is not a `ValueError`, so a hand-edited file
    # with an unclosed bracket escaped `cua.cli`'s `except (FileNotFoundError, ValueError)`
    # as a traceback instead of the one-line refusal every other malformed artifact gets.
    save(base(), tmp_path)
    path = tmp_path / "artifacts" / "corebank.probe" / "v1.yaml"
    path.write_text("schema_version: 1\nid: corebank.probe\nsteps: [\n")
    with pytest.raises(ValueError, match="could not be parsed") as caught:
        load("corebank.probe", 1, tmp_path)
    assert not isinstance(caught.value, yaml.YAMLError)
    assert "v1.yaml" in str(caught.value)


def test_approval_by_id_refuses_a_syntactically_malformed_file_the_same_way(tmp_path) -> None:
    # The other door onto `_parse_artifact`: the approval gate's disk-resolution branch.
    save(base(), tmp_path)
    path = tmp_path / "artifacts" / "corebank.probe" / "v1.yaml"
    path.write_text("steps: [\n")
    with pytest.raises(ValueError, match="could not be parsed"):
        write_registry_entry(tmp_path, "corebank.probe", 1, RegistryEntry(status="approved"))
