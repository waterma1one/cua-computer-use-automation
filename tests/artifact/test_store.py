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
   regular expression (with the one declared exception, `InputSpec.pattern`).
2. `status` and `stability` appear only in `registry.json`, never in the artifact file.
"""

import json

import pytest

from cua.artifact.store import RegistryEntry, load, read_registry, save, write_registry_entry
from tests.artifact.factories import base


def test_a_saved_artifact_round_trips(tmp_path) -> None:
    path = save(base(), tmp_path)
    assert load("corebank.probe", 1, tmp_path) == base()
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
    write_registry_entry(tmp_path, "corebank.probe", 1,
                         RegistryEntry(status="approved", replays=3, successes=3))
    text = save(base(), tmp_path).read_text().lower()
    assert "status" not in text
    assert "stability" not in text
    assert "approved" not in text


def test_the_registry_holds_lifecycle_state_keyed_by_id_and_version(tmp_path) -> None:
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
    from cua.artifact.models import Expect, Matcher
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
    from cua.artifact.models import Expect, Matcher
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
    from cua.artifact.models import Expect, Matcher
    a = base()
    a.steps[0].expects = [
        Expect(when=Matcher(role="heading", name="x"), outcome="continue", source="observed")
    ]
    save(a, tmp_path)
    with pytest.raises(ValueError):
        write_registry_entry(tmp_path, "corebank.probe", 1,
                             RegistryEntry(status="approved"), artifact=a)


def test_an_artifact_with_an_unverified_expect_can_still_be_left_draft(tmp_path) -> None:
    # The gate is specific to `status="approved"`; an unverified artifact must still be
    # storable as a draft. Otherwise a fresh discovery run could never be registered at all.
    from cua.artifact.models import Expect, Matcher
    a = base()
    a.steps[0].expects = [
        Expect(when=Matcher(role="heading", name="x"), outcome="continue", source="proposed")
    ]
    save(a, tmp_path)
    write_registry_entry(tmp_path, "corebank.probe", 1, RegistryEntry(status="draft"), artifact=a)
    registry = read_registry(tmp_path)
    assert registry["corebank.probe"]["1"].status == "draft"


def test_marking_approved_with_no_artifact_supplied_is_not_gated(tmp_path) -> None:
    # `artifact` is optional on `write_registry_entry` -- e.g. an operator flipping a status
    # from the console without recompiling. With no artifact to inspect, the unverified-expect
    # gate cannot run and must not raise; it has nothing to check.
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
    assert load("corebank.probe", 1, tmp_path).version == 1
    assert load("corebank.probe", 2, tmp_path).version == 2


def test_load_surfaces_findings_rather_than_dropping_them(tmp_path) -> None:
    # Task 2's handoff: "validate is the load-time gate -- call it on read, and surface
    # warnings and notes to the operator rather than dropping them." A clean `base()` artifact
    # still carries the E4 ALLOWLIST_NOT_CHECKED note (no DeploymentAllowlist is available to
    # this store), so `load` must expose it rather than silently discarding it.
    save(base(), tmp_path)
    artifact, findings = load("corebank.probe", 1, tmp_path, return_findings=True)
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
