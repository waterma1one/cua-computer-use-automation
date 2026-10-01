import json

import pytest

from cua.artifact.schema import export_tool_schema
from cua.artifact.store import RegistryEntry, read_registry, save, write_registry_entry
from cua.catalog.registry import (
    CatalogRefusal,
    approve,
    describe,
    list_capabilities,
    record_replay,
)
from cua.replay.result import BusinessOutcome, Failure, Success
from tests.artifact.factories import base


def _ok(assistance: str = "none") -> Success:
    return Success(outputs={}, steps_run=["s1"], evidence_ref="evidence/r", assistance=assistance)  # type: ignore[arg-type]


def test_describe_returns_the_cached_export_dict_itself(tmp_path) -> None:
    artifact = base()
    save(artifact, tmp_path)
    first = describe(tmp_path, artifact.id)
    second = describe(tmp_path, artifact.id)
    # D63: one schema, not a copy -- the registry exports once and hands that dict back.
    assert first.schema is second.schema
    assert first.schema == export_tool_schema(artifact)


def test_list_and_describe_report_status_irreversibility_and_unchecked_allowlist(tmp_path) -> None:
    artifact = base()
    artifact.steps[0].risk = "irreversible"
    save(artifact, tmp_path)
    [tool] = list_capabilities(tmp_path)
    assert tool.id == artifact.id and tool.version == 1
    assert tool.status == "draft"  # no registry entry means draft
    assert tool.irreversible is True
    # The load-time ALLOWLIST_NOT_CHECKED note is surfaced, never read as checked-and-clean.
    assert tool.allowlist_checked is False


def test_version_resolution_prefers_latest_approved_else_latest_draft(tmp_path) -> None:
    save(base(version=1), tmp_path)
    save(base(version=2), tmp_path)
    save(base(version=3), tmp_path)
    assert describe(tmp_path, "corebank.probe").version == 3
    approve(tmp_path, "corebank.probe", 2, "ops")
    assert describe(tmp_path, "corebank.probe").version == 2
    assert describe(tmp_path, "corebank.probe", version=3).version == 3


def test_describe_unknown_capability_is_refused(tmp_path) -> None:
    with pytest.raises(CatalogRefusal):
        describe(tmp_path, "nope.nothing")


def test_approve_records_approver_time_and_sandbox_and_preserves_stability(tmp_path) -> None:
    artifact = base()
    save(artifact, tmp_path)
    write_registry_entry(
        tmp_path, artifact.id, 1,
        RegistryEntry(replays=4, successes=3, score=0.75, assisted_replays=1),
    )
    approve(tmp_path, artifact.id, 1, "ops@example")
    entry = read_registry(tmp_path)[artifact.id]["1"]
    assert entry.status == "approved"
    assert entry.approver == "ops@example"
    assert entry.approved_at is not None
    assert entry.sandbox_discovered is True  # base() provenance.policy_mode == "sandbox"
    assert (entry.replays, entry.successes, entry.score, entry.assisted_replays) == (4, 3, 0.75, 1)


def test_approve_requires_an_approver(tmp_path) -> None:
    save(base(), tmp_path)
    with pytest.raises(ValueError):
        approve(tmp_path, "corebank.probe", 1, "  ")


def test_approve_of_a_missing_version_fails(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        approve(tmp_path, "corebank.probe", 9, "ops")


def test_only_unassisted_successes_count_toward_the_score(tmp_path) -> None:
    artifact = base()
    save(artifact, tmp_path)
    record_replay(tmp_path, artifact.id, 1, _ok())
    record_replay(tmp_path, artifact.id, 1, _ok("human"))
    record_replay(tmp_path, artifact.id, 1, _ok("llm_fallback"))
    entry = read_registry(tmp_path)[artifact.id]["1"]
    assert (entry.replays, entry.successes, entry.assisted_replays) == (1, 1, 2)
    assert entry.score == 1.0


def test_business_outcomes_and_failures_are_unassisted_replays_without_success(tmp_path) -> None:
    artifact = base()
    save(artifact, tmp_path)
    record_replay(tmp_path, artifact.id, 1, _ok())
    record_replay(tmp_path, artifact.id, 1, BusinessOutcome(
        code="X", step_id="s1", message="m", evidence_ref="evidence/r"))
    record_replay(tmp_path, artifact.id, 1, Failure(
        kind="LOCATOR_NOT_FOUND", step_id="s1", expected="e", observed="o",
        evidence_ref="evidence/r"))
    entry = read_registry(tmp_path)[artifact.id]["1"]
    assert (entry.replays, entry.successes) == (3, 1)
    assert entry.score == pytest.approx(1 / 3)


def test_an_assisted_only_history_leaves_the_score_unset(tmp_path) -> None:
    save(base(), tmp_path)
    record_replay(tmp_path, "corebank.probe", 1, _ok("human"))
    entry = read_registry(tmp_path)["corebank.probe"]["1"]
    assert entry.score is None and entry.assisted_replays == 1
    assert json.loads((tmp_path / "artifacts" / "registry.json").read_text())
