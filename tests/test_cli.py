import json

from typer.testing import CliRunner

from cua.artifact.models import Expect, InputSpec, Matcher, Step, Target
from cua.artifact.store import save
from cua.cli import app
from tests.artifact.factories import base

runner = CliRunner()


def test_replay_command_reports_a_business_outcome_as_json(tmp_path, live_mockapp) -> None:
    artifact = base()
    artifact.outputs = {}  # this fixture never produces a balance -- base()'s own OutputSpec
                           # would otherwise make load() refuse it as OUTPUT_NEVER_PRODUCED
    artifact.steps = [Step(
        id="s1", action="navigate", target=Target(path="/member/12345?fault=not_found"),
        risk="safe",
        expects=[Expect(
            when=Matcher(strategy="text", name_match="contains", name="No member found"),
            outcome="business", code="MEMBER_NOT_FOUND", source="observed",
        )],
    )]
    save(artifact, tmp_path)
    evidence_root = tmp_path / "evidence_out"

    result = runner.invoke(app, [
        "replay", artifact.id, str(artifact.version),
        "--root", str(tmp_path), "--base-url", live_mockapp,
        "--evidence-root", str(evidence_root),
        "--input", "member_id=12345", "--mode", "embedded",
    ])
    payload = json.loads(result.stdout)
    assert payload["code"] == "MEMBER_NOT_FOUND"
    # E16: the evidence trail is a real side effect of a real run, not only of a
    # hand-called EvidenceWriter method.
    run_dirs = list((evidence_root / "evidence").iterdir())
    assert len(run_dirs) == 1
    assert (run_dirs[0] / "result.json").exists()


def test_supervised_mode_is_a_clean_cli_error(tmp_path) -> None:
    artifact = base()
    save(artifact, tmp_path)
    result = runner.invoke(app, [
        "replay", artifact.id, str(artifact.version),
        "--root", str(tmp_path), "--base-url", "http://127.0.0.1:1",
        "--evidence-root", str(tmp_path / "evidence_out"), "--mode", "supervised",
    ])
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_invalid_input_is_refused_before_a_browser_is_launched(tmp_path, monkeypatch) -> None:
    # E22: the CLI's own real path, not only the engine-level PoisonSurface proof -- a browser
    # is a real resource this test would fail loudly if the CLI paid for regardless of outcome.
    import cua.cli as cli_module
    # base() itself declares no pattern on member_id; the pattern is declared here, the same
    # way tests/replay/test_engine.py and tests/artifact/test_schema_export.py already
    # construct a pattern-constrained member_id input, so this test actually exercises
    # `INVALID_INPUT` rather than a value that a plain `string` input accepts unchanged.
    artifact = base(  # declares member_id: {pattern: "^[0-9]{5}$", required: true}
        inputs={"member_id": InputSpec(type="string", pattern="^[0-9]{5}$", required=True)},
    )
    save(artifact, tmp_path)

    def _must_not_launch(base_url: str):
        raise AssertionError("a browser was launched despite invalid input")

    monkeypatch.setattr(cli_module, "launch_page", _must_not_launch)
    result = runner.invoke(app, [
        "replay", artifact.id, str(artifact.version),
        "--root", str(tmp_path), "--base-url", "http://127.0.0.1:1",
        "--evidence-root", str(tmp_path / "evidence_out"),
        "--input", "member_id=not-five-digits", "--mode", "embedded",
    ])
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "INVALID_INPUT"
