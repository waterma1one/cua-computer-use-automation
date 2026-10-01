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


def _policy(tmp_path, origin):
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump({
        "policy_mode": "sandbox", "allowed_origins": [origin], "allowed_paths": ["/"],
        "denied_paths": [],
        "allowed_actions": ["navigate", "click", "fill", "select", "press_key", "wait_for",
                            "read", "dismiss_dialog"],
    }))
    return str(path)


def test_the_secret_never_reaches_any_file_under_the_evidence_root(
    monkeypatch, tmp_path, live_mockapp,
) -> None:
    script = [
        ToolCall(id="1", name="fill", args={"index": 12, "value": DEFAULT_LOGIN_USER}),
        ToolCall(id="2", name="fill", args={"index": 16, "value": DEFAULT_LOGIN_PASSWORD}),
        ToolCall(id="3", name="click", args={"index": 19}),
        ToolCall(id="4", name="finish", args={"summary": "in", "checkpoint_index": 15}),
    ]
    monkeypatch.setattr(cli_module, "load_gemini_client_from_env",
                        lambda: FakeClient(script=script))
    evidence_root = tmp_path / "evidence_out"
    result = runner.invoke(app, [
        "discover", "--goal", "Log in.", "--root", str(tmp_path / "store"),
        "--base-url", live_mockapp, "--policy", _policy(tmp_path, live_mockapp),
        "--evidence-root", str(evidence_root), "--id", "corebank.login", "--name", "login",
        "--input", f"user={DEFAULT_LOGIN_USER}",
        "--secret-input", f"password={DEFAULT_LOGIN_PASSWORD}",
    ])
    assert result.exit_code == 0, result.output
    files = [p for p in tmp_path.rglob("*") if p.is_file() and p.name != "policy.yaml"]
    assert files
    for path in files:
        assert DEFAULT_LOGIN_PASSWORD.encode() not in path.read_bytes(), path
    assert DEFAULT_LOGIN_PASSWORD not in result.output


def test_the_evidence_root_defaults_outside_the_working_tree() -> None:
    from pathlib import Path

    import typer.main

    command = typer.main.get_command(app).commands["discover"]  # type: ignore[attr-defined]
    default = next(p for p in command.params if p.name == "evidence_root").default
    assert not Path(default).resolve().is_relative_to(Path.cwd().resolve())


def test_self_verify_replays_with_a_different_value_than_discovery_used(
    monkeypatch, tmp_path, live_mockapp,
) -> None:
    script = [
        ToolCall(id="1", name="fill", args={"index": 12, "value": DEFAULT_LOGIN_USER}),
        ToolCall(id="2", name="fill", args={"index": 16, "value": DEFAULT_LOGIN_PASSWORD}),
        ToolCall(id="3", name="click", args={"index": 19}),
        ToolCall(id="4", name="fill", args={"index": 12, "value": "12345"}),
        ToolCall(id="5", name="click", args={"index": 15}),
        ToolCall(id="6", name="finish", args={"summary": "found", "checkpoint_index": 4}),
    ]
    monkeypatch.setattr(cli_module, "load_gemini_client_from_env",
                        lambda: FakeClient(script=script))
    seen: list[dict[str, object]] = []
    real = cli_module.self_verify

    def spy(artifact, base_url, inputs, **kwargs):
        seen.append(dict(inputs))
        return real(artifact, base_url, inputs, **kwargs)

    monkeypatch.setattr(cli_module, "self_verify", spy)
    result = runner.invoke(app, [
        "discover", "--goal", "Find a member.", "--root", str(tmp_path / "store"),
        "--base-url", live_mockapp, "--policy", _policy(tmp_path, live_mockapp),
        "--evidence-root", str(tmp_path / "ev"), "--id", "corebank.find", "--name", "find",
        "--input", f"user={DEFAULT_LOGIN_USER}",
        "--secret-input", f"password={DEFAULT_LOGIN_PASSWORD}",
        "--input", "member_id=12345",
        "--verify-input", "member_id=22222",
    ])
    assert result.exit_code == 0, result.output
    assert seen[0]["member_id"] == "22222"
    assert seen[0]["user"] == DEFAULT_LOGIN_USER
    artifact, _ = load("corebank.find", 1, tmp_path / "store")
    assert artifact.verified is True
    assert "12345" not in artifact.model_dump_json()


def _login_script() -> list[ToolCall]:
    return [
        ToolCall(id="1", name="fill", args={"index": 12, "value": DEFAULT_LOGIN_USER}),
        ToolCall(id="2", name="fill", args={"index": 16, "value": DEFAULT_LOGIN_PASSWORD}),
        ToolCall(id="3", name="click", args={"index": 19}),
        ToolCall(id="4", name="finish", args={"summary": "in", "checkpoint_index": 15}),
    ]


def test_a_secret_in_the_goal_is_refused_at_save_and_nothing_is_written(
    monkeypatch, tmp_path, live_mockapp,
) -> None:
    monkeypatch.setattr(cli_module, "load_gemini_client_from_env",
                        lambda: FakeClient(script=_login_script()))
    store = tmp_path / "store"
    result = runner.invoke(app, [
        "discover", "--goal", f"Log in with password {DEFAULT_LOGIN_PASSWORD}.",
        "--root", str(store), "--base-url", live_mockapp,
        "--policy", _policy(tmp_path, live_mockapp),
        "--evidence-root", str(tmp_path / "ev"), "--id", "corebank.login", "--name", "login",
        "--input", f"user={DEFAULT_LOGIN_USER}",
        "--secret-input", f"password={DEFAULT_LOGIN_PASSWORD}",
    ])
    assert result.exit_code == 1
    assert "declared secret" in result.output
    assert DEFAULT_LOGIN_PASSWORD not in result.output
    assert not (store / "artifacts").exists() or not list((store / "artifacts").rglob("*.yaml"))


def test_a_compile_error_is_masked_before_whitespace_is_collapsed(
    monkeypatch, tmp_path, live_mockapp,
) -> None:
    from cua.artifact.compile import CompileError

    secret = "two  spaces\tand\nnewline"
    monkeypatch.setattr(cli_module, "load_gemini_client_from_env",
                        lambda: FakeClient(script=_login_script()))

    def boom(*args, **kwargs):
        raise CompileError(f"cannot compile near {secret} here")

    monkeypatch.setattr(cli_module, "compile_artifact", boom)
    result = runner.invoke(app, [
        "discover", "--goal", "Log in.", "--root", str(tmp_path / "store"),
        "--base-url", live_mockapp, "--policy", _policy(tmp_path, live_mockapp),
        "--evidence-root", str(tmp_path / "ev"), "--id", "corebank.login", "--name", "login",
        "--input", f"user={DEFAULT_LOGIN_USER}",
        "--secret-input", f"password={secret}",
    ])
    assert result.exit_code == 1
    assert "spaces" not in result.output
    assert "newline" not in result.output
