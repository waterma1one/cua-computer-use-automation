import json

from cua.artifact.models import InputSpec, Provenance
from cua.observability.evidence import EvidenceWriter
from cua.replay.result import Success
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


def test_write_result_serializes_the_replayresult_exactly(tmp_path) -> None:
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
