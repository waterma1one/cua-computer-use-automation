import json

from cua.observability.log import RunLog


def test_event_appends_one_json_object_per_call(tmp_path) -> None:
    log = RunLog(tmp_path / "trace.jsonl")
    log.event(step_id="s1", action="fill", resolution="unique")
    log.event(step_id="s2", action="click", resolution="unique", branch="continue")
    lines = (tmp_path / "trace.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["step_id"] == "s1"
    assert json.loads(lines[1])["branch"] == "continue"


def test_a_snapshot_text_field_is_scrubbed_before_it_reaches_the_line(tmp_path) -> None:
    log = RunLog(tmp_path / "trace.jsonl")
    log.event(step_id="s1", kind="timed_out",
             snapshot_yaml="- textbox \"Password\": hunter2")
    written = (tmp_path / "trace.jsonl").read_text()
    assert "hunter2" not in written
