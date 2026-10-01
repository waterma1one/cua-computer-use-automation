"""The whole pipeline, live, with a scripted FakeClient standing in for the model -- this
phase never requires a real key (acceptance criterion 1); phase 8 is the one real run.
"""
import yaml
from typer.testing import CliRunner

import cua.cli as cli_module
from cua.artifact.store import load, read_registry
from cua.cli import app
from cua.llm.base import ToolCall
from cua.llm.fake import FakeClient
from mockapp.app import DEFAULT_LOGIN_PASSWORD, DEFAULT_LOGIN_USER

runner = CliRunner()


def test_discover_end_to_end_against_the_live_mock_app(
    monkeypatch, tmp_path, live_mockapp,
) -> None:
    script = [
        ToolCall(id="1", name="fill", args={"index": 12, "value": DEFAULT_LOGIN_USER}),
        ToolCall(id="2", name="fill", args={"index": 16, "value": DEFAULT_LOGIN_PASSWORD}),
        ToolCall(id="3", name="click", args={"index": 19}),
        ToolCall(id="4", name="finish", args={
            "summary": "logged in", "checkpoint_index": 15,
        }),
    ]
    monkeypatch.setattr(cli_module, "load_gemini_client_from_env",
                        lambda: FakeClient(script=script))
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(yaml.safe_dump({
        "policy_mode": "sandbox", "allowed_origins": [live_mockapp], "allowed_paths": ["/"],
        "denied_paths": [],
        "allowed_actions": ["navigate", "click", "fill", "select", "press_key", "wait_for",
                            "read", "dismiss_dialog"],
    }))
    evidence_root = tmp_path / "evidence_out"
    result = runner.invoke(app, [
        "discover", "--goal", "Log in.", "--root", str(tmp_path), "--base-url", live_mockapp,
        "--policy", str(policy_path), "--evidence-root", str(evidence_root),
        "--id", "corebank.login", "--name", "login",
        "--input", f"user={DEFAULT_LOGIN_USER}",
        "--secret-input", f"password={DEFAULT_LOGIN_PASSWORD}",
    ])
    assert result.exit_code == 0, result.output
    artifact, findings = load("corebank.login", 1, tmp_path)
    assert artifact.verified is True
    assert [f for f in findings if f.level == "error"] == []
    assert artifact.provenance.trace_ref != ""
    assert artifact.steps[0].action == "navigate"
    assert read_registry(tmp_path)["corebank.login"]["1"].status == "draft"
    run_dirs = list((evidence_root / "evidence").iterdir())
    assert len(run_dirs) == 1
    assert (run_dirs[0] / "trace.jsonl").exists()
