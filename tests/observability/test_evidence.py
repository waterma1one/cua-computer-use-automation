import json

from cua.artifact.models import InputSpec, Provenance
from cua.observability.evidence import EvidenceWriter
from cua.observability.log import RunLog
from cua.replay.result import Failure, Success
from cua.surface.models import EvidenceFrame
from tests.artifact.factories import base


def test_evidence_ref_and_run_id_use_the_documented_shape(tmp_path) -> None:
    writer = EvidenceWriter(tmp_path)
    assert writer.evidence_ref() == f"evidence/{writer.run_id}"


def test_write_run_produces_the_spec_10_layout(tmp_path) -> None:
    writer = EvidenceWriter(tmp_path)
    writer.write_run(goal="look up a balance", capability="corebank.probe",
                     inputs={"member_id": "12345"},
                     input_specs={"member_id": InputSpec(type="string", required=True)},
                     policy_mode="sandbox")
    run_dir = tmp_path / "evidence" / writer.run_id
    data = json.loads((run_dir / "run.json").read_text())
    assert data["capability"] == "corebank.probe"
    assert data["inputs"] == {"member_id": "12345"}


def test_write_run_masks_a_sensitive_input(tmp_path) -> None:
    writer = EvidenceWriter(tmp_path)
    writer.write_run(goal="log in", capability="corebank.probe",
                     inputs={"password": "hunter2"},
                     input_specs={"password": InputSpec(type="string", sensitive=True)},
                     policy_mode="sandbox")
    data = json.loads((tmp_path / "evidence" / writer.run_id / "run.json").read_text())
    assert data["inputs"]["password"] == "[REDACTED]"


def test_write_result_writes_an_unshaped_unredacted_result_through_unchanged(tmp_path) -> None:
    # Not "exactly": Task 1 made `write_result` mask `redact`-flagged outputs and pattern-
    # filter everything it writes. This fixture's value is neither flagged nor PII-shaped,
    # so it survives -- which is the property being pinned here.
    writer = EvidenceWriter(tmp_path)
    result = Success(outputs={"balance": "4218.60"}, steps_run=["s1", "s2"],
                     evidence_ref=writer.evidence_ref())
    writer.write_result(result)
    written = json.loads((tmp_path / "evidence" / writer.run_id / "result.json").read_text())
    assert written["outputs"] == {"balance": "4218.60"}


def test_write_artifact_uses_the_stores_own_dump_helper(tmp_path) -> None:
    from cua.artifact.store import dump_yaml
    writer = EvidenceWriter(tmp_path)
    writer.write_artifact(base())
    written = (tmp_path / "evidence" / writer.run_id / "artifact.yaml").read_text()
    assert written == dump_yaml(base())


def test_frame_writes_both_the_screenshot_and_a_never_protected_snapshot(tmp_path) -> None:
    # STATE.md item 5 / spec §3.7.2: the on-disk assertion phase 2 never wrote.
    writer = EvidenceWriter(tmp_path)
    frame = EvidenceFrame(
        generation=1, image_png=b"\x89PNG",
        snapshot_yaml='- textbox "Password": hunter2\n- textbox "User": teller',
    )
    writer.frame(frame, "s1_failure")
    run_dir = tmp_path / "evidence" / writer.run_id
    assert (run_dir / "screenshots" / "s1_failure.png").exists()
    snapshot = (run_dir / "snapshots" / "s1_failure.yaml").read_text()
    assert "hunter2" not in snapshot
    assert "teller" in snapshot


def test_a_forbidden_content_log_line_never_reaches_the_trace(tmp_path, caplog) -> None:
    import contextlib
    import logging

    from cua.artifact.store import save
    writer = EvidenceWriter(tmp_path / "run_root")
    artifact = base()
    artifact.provenance = Provenance(
        discovered_at="2026-09-09T00:00:00", model="gemini-2.5-flash-lite",
        policy_mode="sandbox", provider_retention="training_permitted",
        run_id="r_01H", trace_ref="http://leaked.example/trace.jsonl",
    )
    caplog.set_level(logging.WARNING)
    with contextlib.suppress(ValueError):
        save(artifact, tmp_path / "store_root")  # expected: FORBIDDEN_CONTENT refuses the save
    writer.event(step_id="s1", action="fill")
    trace_path = tmp_path / "run_root" / "evidence" / writer.run_id / "trace.jsonl"
    assert "leaked.example" not in trace_path.read_text()


def test_write_run_refuses_an_input_with_no_spec_rather_than_guessing(tmp_path) -> None:
    # Final fix wave, ledger minor 10 / E31: an input that no `InputSpec` describes cannot
    # be known to be non-sensitive, so `write_run` raises rather than defaulting it to
    # plain text. The caller (the CLI) refuses undeclared inputs before ever reaching here.
    import pytest

    writer = EvidenceWriter(tmp_path)
    with pytest.raises(KeyError):
        writer.write_run(goal="log in", capability="corebank.probe",
                         inputs={"password": "hunter2"},
                         input_specs={}, policy_mode="sandbox")
    assert not (tmp_path / "evidence" / writer.run_id / "run.json").exists()


# Phase 5 / E8, E9: the writer is the redaction layer. Every text write is pattern-filtered;
# a `redact` output is masked in evidence and untouched in the returned result.

def test_a_snapshot_with_an_ssn_is_shape_redacted_on_write(tmp_path) -> None:
    writer = EvidenceWriter(tmp_path)
    frame = EvidenceFrame(
        generation=1, image_png=None,
        snapshot_yaml="- text: Member 12345 Dana Whitfield SSN 412-55-0198 Acct 000100045512",
    )
    writer.frame(frame, "s1")
    snapshot = (tmp_path / "evidence" / writer.run_id / "snapshots" / "s1.yaml").read_text()
    assert "412-55-0198" not in snapshot
    assert "***-**-0198" in snapshot
    assert "********5512" in snapshot
    assert "Member 12345" in snapshot


def test_a_redact_output_is_masked_in_result_json_and_untouched_in_the_result(
    tmp_path,
) -> None:
    # Criterion 7, both halves on one object: the caller asked for the balance and gets it;
    # the evidence copy is masked, shape kept.
    writer = EvidenceWriter(tmp_path)
    result = Success(outputs={"balance": "4218.60", "kind": "Savings"}, steps_run=["s1"],
                     evidence_ref=writer.evidence_ref())
    written = writer.write_result(result, redacted_outputs={"balance"})
    on_disk = json.loads((tmp_path / "evidence" / writer.run_id / "result.json").read_text())
    assert on_disk == written
    assert on_disk["outputs"] == {"balance": "**18.60", "kind": "Savings"}
    assert result.outputs == {"balance": "4218.60", "kind": "Savings"}


def test_result_json_is_pattern_filtered_even_with_no_redact_outputs(tmp_path) -> None:
    writer = EvidenceWriter(tmp_path)
    failure = Failure(
        kind="NO_BRANCH_MATCHED", step_id="s1", expected="a heading",
        observed="observed: Member 12345 Dana Whitfield SSN 412-55-0198",
        evidence_ref=writer.evidence_ref(),
    )
    writer.write_result(failure)
    text = (tmp_path / "evidence" / writer.run_id / "result.json").read_text()
    assert "412-55-0198" not in text
    assert "***-**-0198" in text
    assert writer.evidence_ref() in text  # the pointer survives the pattern pass


def test_a_bound_event_tagged_redact_is_masked_in_the_trace(tmp_path) -> None:
    log = RunLog(tmp_path / "trace.jsonl")
    log.event(kind="bound", step_id="s2", into="balance", value="4218.60", redact=True)
    log.event(kind="bound", step_id="s3", into="kind", value="Savings", redact=False)
    lines = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert lines[0]["value"] == "**18.60"
    assert lines[1]["value"] == "Savings"


def test_every_string_field_of_an_event_is_pattern_filtered(tmp_path) -> None:
    log = RunLog(tmp_path / "trace.jsonl")
    log.event(kind="dialog", message="Confirm transfer from 000100045512-01?",
              nested={"ssn": "412-55-0198"})
    text = (tmp_path / "trace.jsonl").read_text()
    assert "000100045512" not in text and "412-55-0198" not in text
    assert "********5512-01" in text and "***-**-0198" in text


def test_write_result_with_a_bare_int_account_output_produces_valid_json(tmp_path) -> None:
    # Fix round 1: an unmasked int leaf that happens to be account-shaped would otherwise
    # survive redact_leaves as a number, and the text-level pattern pass in
    # RedactingWriter would then rewrite its unquoted digits in place, corrupting the JSON.
    writer = EvidenceWriter(tmp_path)
    result = Success(outputs={"acct": 100045512345}, steps_run=["s1"],
                     evidence_ref=writer.evidence_ref())
    written = writer.write_result(result)
    on_disk = json.loads((tmp_path / "evidence" / writer.run_id / "result.json").read_text())
    assert on_disk == written
    assert on_disk["outputs"] == {"acct": "********2345"}


def test_a_bare_int_event_field_is_masked_to_a_string_and_stays_valid_json(tmp_path) -> None:
    log = RunLog(tmp_path / "trace.jsonl")
    log.event(kind="tick", ts=175802400012)
    line = json.loads((tmp_path / "trace.jsonl").read_text().strip())
    assert line["ts"] == "********0012"


def test_a_non_sensitive_input_that_is_account_shaped_is_masked_in_run_json(tmp_path) -> None:
    # E8's accepted cost, pinned so it is a decision and not a surprise: the pattern layer
    # runs over run.json too. Shape kept, last four kept.
    writer = EvidenceWriter(tmp_path)
    writer.write_run(goal="g", capability="c",
                     inputs={"acct": "000100045512", "member_id": "12345"},
                     input_specs={"acct": InputSpec(type="string"),
                                  "member_id": InputSpec(type="string")},
                     policy_mode="strict")
    data = json.loads((tmp_path / "evidence" / writer.run_id / "run.json").read_text())
    assert data["inputs"] == {"acct": "********5512", "member_id": "12345"}


def test_event_and_frame_never_write_a_declared_secret(tmp_path) -> None:
    from cua.observability.evidence import EvidenceWriter
    from cua.surface.models import EvidenceFrame

    writer = EvidenceWriter(tmp_path, secrets=["s3cr3t-value"])
    writer.event(kind="act", args={"value": "s3cr3t-value", "nested": ["x s3cr3t-value y"]})
    writer.frame(EvidenceFrame(generation=1, snapshot_yaml="- textbox: s3cr3t-value\n"), "f")
    texts = [p.read_text() for p in tmp_path.rglob("*") if p.is_file()]
    assert texts and all("s3cr3t-value" not in t for t in texts)
    assert any("[REDACTED]" in t for t in texts)


def test_overlapping_secrets_are_masked_longest_first(tmp_path) -> None:
    from cua.observability.evidence import EvidenceWriter, mask_secrets

    assert mask_secrets("pw=hunter22", ["hunter", "hunter22", "", "hunter"]) == "pw=[REDACTED]"
    writer = EvidenceWriter(tmp_path, secrets=["hunter", "hunter22"])
    writer.event(kind="act", value="hunter22")
    text = next(tmp_path.rglob("trace.jsonl")).read_text()
    assert "22" not in text.replace("[REDACTED]", "")
