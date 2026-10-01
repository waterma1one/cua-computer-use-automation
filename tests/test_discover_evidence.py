"""`cua discover` leaves a full evidence directory for every run (phase 8, criteria 1/5):
run.json, trace.jsonl, result.json, artifact.yaml (when saved), screenshots and snapshots,
on success and on refusal/failure alike -- and none of it holds a secret or an SSN.
"""
import json
import re

import yaml
from typer.testing import CliRunner

import cua.cli as cli_module
from cua.artifact.compile import CompileError
from cua.cli import app
from cua.llm.base import ToolCall, Usage
from cua.llm.fake import FakeClient
from mockapp.app import DEFAULT_LOGIN_PASSWORD, DEFAULT_LOGIN_USER

runner = CliRunner()
SSN = "331-08-7742"
_SSN_RE = re.compile(r"\d{3}-\d{2}-\d{4}")

LOGIN = [
    ToolCall(id="1", name="fill", args={"index": 12, "value": DEFAULT_LOGIN_USER}),
    ToolCall(id="2", name="fill", args={"index": 16, "value": DEFAULT_LOGIN_PASSWORD}),
    ToolCall(id="3", name="click", args={"index": 19}),
]
FINISH = ToolCall(id="4", name="finish", args={"summary": "in", "checkpoint_index": 15})


def _invoke(monkeypatch, tmp_path, origin, script, step_usage=None):
    monkeypatch.setattr(cli_module, "load_gemini_client_from_env",
                        lambda: FakeClient(script=script, step_usage=step_usage))
    policy = tmp_path / "policy.yaml"
    policy.write_text(yaml.safe_dump({
        "policy_mode": "sandbox", "allowed_origins": [origin], "allowed_paths": ["/"],
        "denied_paths": [],
        "allowed_actions": ["navigate", "click", "fill", "select", "press_key", "wait_for",
                            "read", "dismiss_dialog"],
    }))
    result = runner.invoke(app, [
        "discover", "--goal", "Log in.", "--root", str(tmp_path / "store"),
        "--base-url", origin, "--policy", str(policy),
        "--evidence-root", str(tmp_path / "ev"), "--id", "corebank.login", "--name", "login",
        "--input", f"user={DEFAULT_LOGIN_USER}",
        "--secret-input", f"password={DEFAULT_LOGIN_PASSWORD}",
    ])
    (run_dir,) = list((tmp_path / "ev" / "evidence").iterdir())
    return result, run_dir


def _assert_clean(tmp_path, run_dir, result) -> None:
    for path in run_dir.rglob("*"):
        if path.is_file() and path.suffix != ".png":
            text = path.read_text()
            assert DEFAULT_LOGIN_PASSWORD not in text, path
            assert not _SSN_RE.search(text), path
    assert DEFAULT_LOGIN_PASSWORD not in result.output
    assert SSN not in result.output


def test_a_saved_run_writes_the_whole_evidence_set(monkeypatch, tmp_path, live_mockapp) -> None:
    result, run_dir = _invoke(monkeypatch, tmp_path, live_mockapp, [*LOGIN, FINISH],
                              step_usage=Usage(prompt=10, completion=5, total=15))
    assert result.exit_code == 0, result.output
    for name in ("run.json", "trace.jsonl", "result.json", "artifact.yaml"):
        assert (run_dir / name).is_file(), name
    assert list((run_dir / "screenshots").glob("*.png"))
    assert list((run_dir / "snapshots").glob("*.yaml"))
    run = json.loads((run_dir / "run.json").read_text())
    assert run["goal"] == "Log in."
    assert run["capability"] == {"id": "corebank.login", "name": "login"}
    assert run["inputs"]["password"] == "[REDACTED]"
    assert run["inputs"]["user"] == DEFAULT_LOGIN_USER
    assert run["tokens"] == {"prompt": 40, "completion": 20, "total": 60}
    assert run["policy_mode"] == "sandbox"
    assert run["base_url"] == live_mockapp
    assert run["step_count"] == 3
    assert run["started_at"] and run["ended_at"]
    assert "estimated_cost_usd" in run and run["model"] == "unknown"
    assert run["estimated_cost_usd"] is None and run["cost_note"]
    outcome = json.loads((run_dir / "result.json").read_text())
    assert outcome["outcome"] == "saved"
    assert outcome["verified"] is True
    assert outcome["stop_reason"] == "finish"
    assert outcome["artifact"]["id"] == "corebank.login"
    _assert_clean(tmp_path, run_dir, result)


def test_a_refused_run_still_leaves_run_and_result(monkeypatch, tmp_path, live_mockapp) -> None:
    give_up = ToolCall(id="9", name="give_up", args={"reason": f"saw {SSN} and gave up"})
    result, run_dir = _invoke(monkeypatch, tmp_path, live_mockapp, [give_up])
    assert result.exit_code == 1
    assert (run_dir / "run.json").is_file()
    assert (run_dir / "trace.jsonl").is_file()
    assert not (run_dir / "artifact.yaml").exists()
    assert list((run_dir / "snapshots").glob("*.yaml"))
    outcome = json.loads((run_dir / "result.json").read_text())
    assert outcome["outcome"] == "refused"
    assert outcome["stop_reason"] == "give_up"
    assert outcome["verified"] is False
    assert outcome["artifact"] is None
    _assert_clean(tmp_path, run_dir, result)


def test_a_self_verify_failure_is_masked_everywhere(monkeypatch, tmp_path, live_mockapp) -> None:
    def failing(*_a, **_k):
        raise CompileError(
            f"self-verification failed at step 's3': expected 'x', observed 'SSN {SSN}' "
            f"and {DEFAULT_LOGIN_PASSWORD}"
        )

    monkeypatch.setattr(cli_module, "self_verify", failing)
    result, run_dir = _invoke(monkeypatch, tmp_path, live_mockapp, [*LOGIN, FINISH])
    assert result.exit_code == 1
    outcome = json.loads((run_dir / "result.json").read_text())
    assert outcome["outcome"] == "failed"
    assert outcome["verified"] is False
    assert "self-verification failed" in outcome["failure_message"]
    assert (run_dir / "run.json").is_file()
    _assert_clean(tmp_path, run_dir, result)
