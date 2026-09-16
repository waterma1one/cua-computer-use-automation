import json
from contextlib import contextmanager

import yaml
from typer.testing import CliRunner

from cua.artifact.models import (
    Expect,
    InputSpec,
    Locator,
    Matcher,
    OutputSpec,
    Settle,
    Step,
    SurfaceSegment,
    Target,
)
from cua.artifact.models import Success as ArtifactSuccess
from cua.artifact.store import save
from cua.cli import app
from cua.replay.result import Success
from tests.artifact.factories import base, loc

runner = CliRunner()


def _policy_file(tmp_path, origin: str = "http://127.0.0.1:1", **over) -> str:
    """A permissive policy for `origin`, written under `tmp_path`. Every invocation passes
    `--policy` (E12): an unattended replay never runs without a deployment allowlist."""
    fields = {
        "policy_mode": "strict", "allowed_origins": [origin], "allowed_paths": ["/"],
        "denied_paths": ["/account/close"],
        "allowed_actions": ["navigate", "click", "fill", "select", "press_key", "wait_for",
                            "read", "dismiss_dialog"],
    }
    fields.update(over)
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(fields))
    return str(path)


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
        "--policy", _policy_file(tmp_path, live_mockapp),
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


def test_evidence_root_defaults_to_the_current_directory(tmp_path, monkeypatch) -> None:
    # E23: --evidence-root defaults to "." so the on-disk layout is evidence/<run_id>/ at
    # the cwd -- not evidence/evidence/<run_id>/, which is what a default of "evidence/"
    # would produce (EvidenceWriter's own root/evidence/<run_id> already adds the
    # "evidence" segment). No real browser is needed to pin a path, so `launch_page` and
    # the engine's `replay` are both faked here.
    import cua.cli as cli_module

    artifact = base()
    save(artifact, tmp_path)
    monkeypatch.chdir(tmp_path)

    class _FakeContext:
        def route(self, *_args: object, **_kwargs: object) -> None:
            pass

        def on(self, *_args: object, **_kwargs: object) -> None:
            pass

    class _FakePage:
        context = _FakeContext()

        def on(self, *_args: object, **_kwargs: object) -> None:
            pass

    @contextmanager
    def _fake_launch_page(base_url: str):
        yield _FakePage()

    def _fake_replay(artifact, inputs, surface, mode, *, evidence=None, **_kwargs):
        assert evidence is not None
        return Success(outputs={}, steps_run=[], evidence_ref=evidence.evidence_ref())

    monkeypatch.setattr(cli_module, "launch_page", _fake_launch_page)
    monkeypatch.setattr(cli_module, "run_replay", _fake_replay)

    result = runner.invoke(app, [
        "replay", artifact.id, str(artifact.version),
        "--root", str(tmp_path), "--base-url", "http://127.0.0.1:1",
        "--policy", _policy_file(tmp_path),
        "--input", "member_id=12345", "--mode", "embedded",
    ])
    assert result.exit_code == 0, result.stdout
    assert not (tmp_path / "evidence" / "evidence").exists()
    run_dirs = list((tmp_path / "evidence").iterdir())
    assert len(run_dirs) == 1
    assert run_dirs[0].name.startswith("run-")
    assert (run_dirs[0] / "result.json").exists()


def test_supervised_mode_is_a_clean_cli_error(tmp_path) -> None:
    artifact = base()
    save(artifact, tmp_path)
    result = runner.invoke(app, [
        "replay", artifact.id, str(artifact.version),
        "--root", str(tmp_path), "--base-url", "http://127.0.0.1:1",
        "--policy", _policy_file(tmp_path),
        "--evidence-root", str(tmp_path / "evidence_out"), "--mode", "supervised",
    ])
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_invalid_input_is_refused_before_a_browser_is_launched(tmp_path, monkeypatch) -> None:
    # E22: the CLI's own real path, not only the engine-level PoisonSurface proof -- a browser
    # is a real resource this test would fail loudly if the CLI paid for regardless of outcome.
    import cua.cli as cli_module
    # base() declares no pattern on member_id, so the override below is what makes
    # "not-five-digits" invalid -- without it, a plain `string` input accepts that value
    # unchanged and this test would never reach INVALID_INPUT.
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
        "--policy", _policy_file(tmp_path),
        "--evidence-root", str(tmp_path / "evidence_out"),
        "--input", "member_id=not-five-digits", "--mode", "embedded",
    ])
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "INVALID_INPUT"
    # E33 reversed E16's "writer after validate": the refusal now leaves a record under
    # the evidence_ref it printed, while E22's "no browser" above is unchanged.
    assert (tmp_path / "evidence_out" / payload["evidence_ref"] / "result.json").exists()


def test_a_missing_artifact_is_a_clean_cli_error(tmp_path, monkeypatch) -> None:
    # Fix round 2, item 1: `load()` raising `FileNotFoundError` used to be an uncaught
    # traceback with empty stdout -- caught and reported on stderr instead, exit 2, well
    # before any browser is launched.
    import cua.cli as cli_module

    def _must_not_launch(base_url: str):
        raise AssertionError("a browser was launched despite a missing artifact")

    monkeypatch.setattr(cli_module, "launch_page", _must_not_launch)
    result = runner.invoke(app, [
        "replay", "corebank.probe", "1",
        "--root", str(tmp_path), "--base-url", "http://127.0.0.1:1",
        "--policy", _policy_file(tmp_path),
        "--evidence-root", str(tmp_path / "evidence_out"),
    ])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr != ""


def test_an_artifact_that_fails_to_load_is_a_clean_cli_error(tmp_path, monkeypatch) -> None:
    # Fix round 2, item 1: an artifact that saves cleanly (FAIL_CODE_NOT_A_FAILURE_KIND is
    # not in CRITERION_1_CODES) but refuses to load (it is an error-level finding) used to
    # raise `ValueError` straight through the CLI -- same fixture shape as
    # tests/artifact/test_store.py::test_a_fail_clause_with_no_code_saves_but_refuses_to_load.
    import cua.cli as cli_module

    artifact = base(steps=[
        Step(id="s1", action="click", locator=loc("x"), risk="safe",
             expects=[Expect(when=Matcher(role="heading", name="x"),
                             outcome="fail", source="observed")]),
        Step(id="s2", action="read", locator=loc("y"), extract="text",
             into="balance", risk="safe"),
    ])
    save(artifact, tmp_path)

    def _must_not_launch(base_url: str):
        raise AssertionError("a browser was launched despite a load-time refusal")

    monkeypatch.setattr(cli_module, "launch_page", _must_not_launch)
    result = runner.invoke(app, [
        "replay", artifact.id, str(artifact.version),
        "--root", str(tmp_path), "--base-url", "http://127.0.0.1:1",
        "--policy", _policy_file(tmp_path),
        "--evidence-root", str(tmp_path / "evidence_out"),
    ])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "FAIL_CODE_NOT_A_FAILURE_KIND" in result.stderr


def test_an_uncoercible_integer_input_is_invalid_input_not_a_traceback(
    tmp_path, monkeypatch
) -> None:
    # Fix round 2, item 2: `int(value)` for a `type: integer` input used to be an uncaught
    # traceback on a bad `--input`. member_id stays declared so the fixture's own steps
    # (which draw from it via `from_input`) still pass load-time validation; `count` is the
    # integer input this test actually exercises.
    import cua.cli as cli_module

    artifact = base(inputs={
        "member_id": InputSpec(type="string", required=True),
        "count": InputSpec(type="integer", required=True),
    })
    save(artifact, tmp_path)

    def _must_not_launch(base_url: str):
        raise AssertionError("a browser was launched despite an uncoercible input")

    monkeypatch.setattr(cli_module, "launch_page", _must_not_launch)
    result = runner.invoke(app, [
        "replay", artifact.id, str(artifact.version),
        "--root", str(tmp_path), "--base-url", "http://127.0.0.1:1",
        "--policy", _policy_file(tmp_path),
        "--evidence-root", str(tmp_path / "evidence_out"),
        "--input", "count=abc", "--mode", "embedded",
    ])
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "INVALID_INPUT"


def test_a_malformed_input_pair_without_an_equals_sign_is_invalid_input(
    tmp_path, monkeypatch
) -> None:
    # Fix round 2, item 3: `pair.partition("=")` on a `--input` with no `=` used to
    # silently produce key="member_id", value="", which passes an unpatterned input and
    # reaches the browser -- E22's failure via a malformed flag rather than a bad value.
    import cua.cli as cli_module

    artifact = base()
    save(artifact, tmp_path)

    def _must_not_launch(base_url: str):
        raise AssertionError("a browser was launched despite a malformed --input pair")

    monkeypatch.setattr(cli_module, "launch_page", _must_not_launch)
    result = runner.invoke(app, [
        "replay", artifact.id, str(artifact.version),
        "--root", str(tmp_path), "--base-url", "http://127.0.0.1:1",
        "--policy", _policy_file(tmp_path),
        "--evidence-root", str(tmp_path / "evidence_out"),
        "--input", "member_id", "--mode", "embedded",
    ])
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "INVALID_INPUT"


# Final fix wave, C1 / E31: a sensitive input's value leaks nowhere -- not stdout, not any
# file under the evidence directory.

def _every_file_under(root) -> dict[str, str]:
    return {str(p.relative_to(root)): p.read_text() for p in root.rglob("*") if p.is_file()}


def test_a_sensitive_inputs_value_reaches_neither_stdout_nor_the_evidence_dir(
    tmp_path, monkeypatch
) -> None:
    # The whole-phase reviewer's construction: a `sensitive: true` member_id failing its
    # pattern used to print `"observed":"input 'member_id' was 'SECRET1'"` to stdout and
    # into result.json.
    import cua.cli as cli_module

    artifact = base(inputs={"member_id": InputSpec(
        type="string", pattern="^[0-9]{5}$", required=True, sensitive=True,
    )})
    save(artifact, tmp_path)
    evidence_root = tmp_path / "evidence_out"

    def _must_not_launch(base_url: str):
        raise AssertionError("a browser was launched despite invalid input")

    monkeypatch.setattr(cli_module, "launch_page", _must_not_launch)
    result = runner.invoke(app, [
        "replay", artifact.id, str(artifact.version),
        "--root", str(tmp_path), "--base-url", "http://127.0.0.1:1",
        "--policy", _policy_file(tmp_path),
        "--evidence-root", str(evidence_root),
        "--input", "member_id=SECRET1", "--mode", "embedded",
    ])
    assert result.exit_code == 1
    assert "SECRET1" not in result.stdout
    payload = json.loads(result.stdout)
    assert payload["kind"] == "INVALID_INPUT"

    written = _every_file_under(evidence_root)
    assert any(name.endswith("run.json") for name in written), sorted(written)
    assert any(name.endswith("result.json") for name in written), sorted(written)
    for name, text in written.items():
        assert "SECRET1" not in text, name
    run_json = next(text for name, text in written.items() if name.endswith("run.json"))
    assert json.loads(run_json)["inputs"]["member_id"] == "[REDACTED]"


def test_a_sensitive_uncoercible_integer_never_reaches_stdout(tmp_path, monkeypatch) -> None:
    # The CLI's own construction site (`_coerce_inputs`), not only the engine's.
    import cua.cli as cli_module

    artifact = base(inputs={
        "member_id": InputSpec(type="string", required=True),
        "pin": InputSpec(type="integer", required=True, sensitive=True),
    })
    save(artifact, tmp_path)

    def _must_not_launch(base_url: str):
        raise AssertionError("a browser was launched despite an uncoercible input")

    monkeypatch.setattr(cli_module, "launch_page", _must_not_launch)
    result = runner.invoke(app, [
        "replay", artifact.id, str(artifact.version),
        "--root", str(tmp_path), "--base-url", "http://127.0.0.1:1",
        "--policy", _policy_file(tmp_path),
        "--evidence-root", str(tmp_path / "evidence_out"),
        "--input", "member_id=12345", "--input", "pin=SECRETPIN", "--mode", "embedded",
    ])
    assert result.exit_code == 1
    assert "SECRETPIN" not in result.stdout
    payload = json.loads(result.stdout)
    assert payload["kind"] == "INVALID_INPUT"
    assert "[REDACTED]" in payload["observed"]
    for name, text in _every_file_under(tmp_path / "evidence_out").items():
        assert "SECRETPIN" not in text, name


# Final fix wave, I1 / E32: --mode is validated against Mode's literals before any I/O.

def test_an_unknown_mode_is_refused_before_the_artifact_is_even_loaded(
    tmp_path, monkeypatch
) -> None:
    # `--mode bogus` used to sail past both `== "supervised"` checks into an unattended
    # embedded replay.
    import cua.cli as cli_module

    artifact = base()
    save(artifact, tmp_path)

    def _must_not_load(*_args: object, **_kwargs: object):
        raise AssertionError("the artifact was loaded despite an unknown --mode")

    def _must_not_launch(base_url: str):
        raise AssertionError("a browser was launched despite an unknown --mode")

    monkeypatch.setattr(cli_module, "load", _must_not_load)
    monkeypatch.setattr(cli_module, "launch_page", _must_not_launch)
    result = runner.invoke(app, [
        "replay", artifact.id, str(artifact.version),
        "--root", str(tmp_path), "--base-url", "http://127.0.0.1:1",
        "--policy", _policy_file(tmp_path),
        "--evidence-root", str(tmp_path / "evidence_out"),
        "--input", "member_id=12345", "--mode", "bogus",
    ])
    # Same shape and same exit code as the `supervised` refusal.
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.strip() != ""
    assert result.stderr.count("\n") == 1
    assert not (tmp_path / "evidence_out").exists()


# Final fix wave, I2 / E33: a pre-browser refusal still leaves an evidence record under
# the evidence_ref it prints.

def test_a_pre_browser_refusal_leaves_run_and_result_json_under_the_printed_ref(
    tmp_path, monkeypatch
) -> None:
    import cua.cli as cli_module

    artifact = base(
        inputs={"member_id": InputSpec(type="string", pattern="^[0-9]{5}$", required=True)},
    )
    save(artifact, tmp_path)
    evidence_root = tmp_path / "evidence_out"

    def _must_not_launch(base_url: str):
        raise AssertionError("a browser was launched despite invalid input")

    monkeypatch.setattr(cli_module, "launch_page", _must_not_launch)
    result = runner.invoke(app, [
        "replay", artifact.id, str(artifact.version),
        "--root", str(tmp_path), "--base-url", "http://127.0.0.1:1",
        "--policy", _policy_file(tmp_path),
        "--evidence-root", str(evidence_root),
        "--input", "member_id=not-five-digits", "--mode", "embedded",
    ])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["kind"] == "INVALID_INPUT"
    run_dir = evidence_root / payload["evidence_ref"]
    assert run_dir.is_dir(), payload["evidence_ref"]
    assert json.loads((run_dir / "run.json").read_text())["inputs"] == {
        "member_id": "not-five-digits"
    }
    assert json.loads((run_dir / "result.json").read_text()) == payload
    # E21: no browser, no frame -- and no artifact copy for a run that executed nothing.
    assert not (run_dir / "screenshots").exists()
    assert not (run_dir / "snapshots").exists()
    assert not (run_dir / "artifact.yaml").exists()


def test_the_writer_is_constructed_before_validate_inputs_and_launch_after(
    tmp_path, monkeypatch
) -> None:
    # The NEW order: EvidenceWriter -> write_run -> validate_inputs -> launch_page. E22's
    # "validate strictly before launch_page" is unchanged; E16's "writer after validate"
    # is reversed (E33).
    import cua.cli as cli_module

    artifact = base()
    save(artifact, tmp_path)
    calls: list[str] = []

    real_writer = cli_module.EvidenceWriter

    class _RecordingWriter(real_writer):
        def __init__(self, root) -> None:
            calls.append("writer")
            super().__init__(root)

        def write_run(self, **kwargs) -> None:
            calls.append("write_run")
            super().write_run(**kwargs)

    def _recording_validate(artifact, inputs):
        calls.append("validate_inputs")
        return None

    class _FakeContext:
        def route(self, *_args: object, **_kwargs: object) -> None:
            pass

        def on(self, *_args: object, **_kwargs: object) -> None:
            pass

    class _FakePage:
        context = _FakeContext()

        def on(self, *_args: object, **_kwargs: object) -> None:
            pass

    @contextmanager
    def _fake_launch_page(base_url: str):
        calls.append("launch_page")
        yield _FakePage()

    def _fake_replay(artifact, inputs, surface, mode, *, evidence=None, **_kwargs):
        return Success(outputs={}, steps_run=[], evidence_ref=evidence.evidence_ref())

    monkeypatch.setattr(cli_module, "EvidenceWriter", _RecordingWriter)
    monkeypatch.setattr(cli_module, "validate_inputs", _recording_validate)
    monkeypatch.setattr(cli_module, "launch_page", _fake_launch_page)
    monkeypatch.setattr(cli_module, "run_replay", _fake_replay)
    result = runner.invoke(app, [
        "replay", artifact.id, str(artifact.version),
        "--root", str(tmp_path), "--base-url", "http://127.0.0.1:1",
        "--policy", _policy_file(tmp_path),
        "--evidence-root", str(tmp_path / "evidence_out"),
        "--input", "member_id=12345", "--mode", "embedded",
    ])
    assert result.exit_code == 0, result.stdout
    assert calls == ["writer", "write_run", "validate_inputs", "launch_page"]


def test_an_undeclared_input_is_invalid_input_not_a_traceback(tmp_path, monkeypatch) -> None:
    # `write_run` now raises KeyError on an input with no InputSpec (E31), so the CLI must
    # refuse an undeclared `--input` before it ever reaches the writer.
    import cua.cli as cli_module

    artifact = base()
    save(artifact, tmp_path)

    def _must_not_launch(base_url: str):
        raise AssertionError("a browser was launched despite an undeclared input")

    monkeypatch.setattr(cli_module, "launch_page", _must_not_launch)
    result = runner.invoke(app, [
        "replay", artifact.id, str(artifact.version),
        "--root", str(tmp_path), "--base-url", "http://127.0.0.1:1",
        "--policy", _policy_file(tmp_path),
        "--evidence-root", str(tmp_path / "evidence_out"),
        "--input", "member_id=12345", "--input", "extra=1", "--mode", "embedded",
    ])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["kind"] == "INVALID_INPUT"
    assert "extra" in payload["observed"]
    run_dir = tmp_path / "evidence_out" / payload["evidence_ref"]
    assert (run_dir / "result.json").exists()


# --- Phase 5 / E12: the policy is required, checked before the browser, and wired in -------

def test_a_missing_policy_flag_is_a_usage_error(tmp_path) -> None:
    artifact = base()
    save(artifact, tmp_path)
    result = runner.invoke(app, [
        "replay", artifact.id, str(artifact.version), "--root", str(tmp_path),
        "--base-url", "http://127.0.0.1:1", "--input", "member_id=12345",
    ])
    assert result.exit_code == 2
    assert "--policy" in result.output


def test_an_unloadable_policy_is_a_clean_exit_2_with_no_evidence(tmp_path, monkeypatch) -> None:
    import cua.cli as cli_module
    artifact = base()
    save(artifact, tmp_path)
    bad = tmp_path / "policy.yaml"
    bad.write_text("allow_paths: ['/']\n")
    def _must_not_launch(base_url: str):
        raise AssertionError("a browser was launched despite a bad policy")

    monkeypatch.setattr(cli_module, "launch_page", _must_not_launch)
    result = runner.invoke(app, [
        "replay", artifact.id, str(artifact.version), "--root", str(tmp_path),
        "--base-url", "http://127.0.0.1:1", "--policy", str(bad),
        "--evidence-root", str(tmp_path / "evidence_out"), "--input", "member_id=12345",
    ])
    assert result.exit_code == 2
    assert "allow_paths" in result.output
    assert not (tmp_path / "evidence_out").exists()


def test_a_widening_capability_policy_is_refused_at_the_cli_with_exit_2(
    tmp_path, monkeypatch,
) -> None:
    # Criterion 4 at the real path: `load()` has no allowlist; the CLI does.
    import cua.cli as cli_module
    from cua.artifact.models import CapabilityPolicy
    artifact = base(policy=CapabilityPolicy(allowed_paths=["/account/close"]))
    save(artifact, tmp_path)
    def _must_not_launch(base_url: str):
        raise AssertionError("a browser was launched despite a widening policy")

    monkeypatch.setattr(cli_module, "launch_page", _must_not_launch)
    result = runner.invoke(app, [
        "replay", artifact.id, str(artifact.version), "--root", str(tmp_path),
        "--base-url", "http://127.0.0.1:1", "--policy", _policy_file(tmp_path),
        "--evidence-root", str(tmp_path / "evidence_out"), "--input", "member_id=12345",
    ])
    assert result.exit_code == 2
    assert "POLICY_WIDENS_ALLOWLIST" in result.output
    assert not (tmp_path / "evidence_out").exists()


def test_the_cli_hands_the_engine_the_narrowed_allowlist_the_registry_status_and_a_guard(
    tmp_path, monkeypatch,
) -> None:
    import cua.cli as cli_module
    from cua.artifact.models import CapabilityPolicy
    from cua.artifact.store import RegistryEntry, write_registry_entry

    artifact = base(policy=CapabilityPolicy(allowed_paths=["/member/"],
                                            allowed_actions=["read", "fill"]))
    save(artifact, tmp_path)
    write_registry_entry(tmp_path, artifact.id, artifact.version, RegistryEntry(status="draft"))
    seen: dict = {}

    class _FakeContext:
        def route(self, pattern, handler) -> None:
            seen["routed"] = pattern

        def on(self, *_args: object, **_kwargs: object) -> None:
            pass

    class _FakePage:
        context = _FakeContext()

        def on(self, *_args: object, **_kwargs: object) -> None:
            pass

    @contextmanager
    def _fake_launch_page(base_url: str):
        yield _FakePage()

    def _fake_replay(artifact, inputs, surface, mode, *, deployment=None, status="draft",
                     evidence=None, **_kwargs):
        seen["deployment"] = deployment
        seen["status"] = status
        seen["guard"] = surface._guard
        return Success(outputs={}, steps_run=[], evidence_ref=evidence.evidence_ref())

    monkeypatch.setattr(cli_module, "launch_page", _fake_launch_page)
    monkeypatch.setattr(cli_module, "run_replay", _fake_replay)
    result = runner.invoke(app, [
        "replay", artifact.id, str(artifact.version), "--root", str(tmp_path),
        "--base-url", "http://127.0.0.1:1", "--policy", _policy_file(tmp_path),
        "--evidence-root", str(tmp_path / "evidence_out"), "--input", "member_id=12345",
    ])
    assert result.exit_code == 0, result.output
    assert seen["deployment"].allowed_paths == ["/member/"]
    assert seen["deployment"].allowed_actions == ["read", "fill"]
    assert seen["deployment"].denied_paths == ["/account/close"]
    assert seen["status"] == "draft"
    assert seen["routed"] == "**/*"
    assert seen["guard"]("http://127.0.0.1:1/member/1") is None
    assert seen["guard"]("http://127.0.0.1:1/account/close") is not None


def test_an_approved_registry_entry_reaches_the_engine_as_approved(tmp_path, monkeypatch) -> None:
    import cua.cli as cli_module
    from cua.artifact.store import RegistryEntry, write_registry_entry

    artifact = base(verified=True)
    save(artifact, tmp_path)
    write_registry_entry(tmp_path, artifact.id, artifact.version, RegistryEntry(status="approved"))
    seen: dict = {}

    class _FakeContext:
        def route(self, *_args: object, **_kwargs: object) -> None:
            pass

        def on(self, *_args: object, **_kwargs: object) -> None:
            pass

    class _FakePage:
        context = _FakeContext()

        def on(self, *_args: object, **_kwargs: object) -> None:
            pass

    @contextmanager
    def _fake_launch_page(base_url: str):
        yield _FakePage()

    def _fake_replay(artifact, inputs, surface, mode, *, status="draft", evidence=None, **_kw):
        seen["status"] = status
        return Success(outputs={}, steps_run=[], evidence_ref=evidence.evidence_ref())

    monkeypatch.setattr(cli_module, "launch_page", _fake_launch_page)
    monkeypatch.setattr(cli_module, "run_replay", _fake_replay)
    result = runner.invoke(app, [
        "replay", artifact.id, str(artifact.version), "--root", str(tmp_path),
        "--base-url", "http://127.0.0.1:1", "--policy", _policy_file(tmp_path),
        "--evidence-root", str(tmp_path / "evidence_out"), "--input", "member_id=12345",
    ])
    assert result.exit_code == 0, result.output
    assert seen["status"] == "approved"


def test_a_redact_output_is_masked_in_result_json_and_printed_to_the_caller(
    tmp_path, monkeypatch,
) -> None:
    # Criterion 7 at the CLI: stdout is the caller's channel; result.json is evidence.
    import cua.cli as cli_module

    artifact = base(outputs={"balance": OutputSpec(type="string", format="money", redact=True)})
    save(artifact, tmp_path)

    class _FakeContext:
        def route(self, *_args: object, **_kwargs: object) -> None:
            pass

        def on(self, *_args: object, **_kwargs: object) -> None:
            pass

    class _FakePage:
        context = _FakeContext()

        def on(self, *_args: object, **_kwargs: object) -> None:
            pass

    @contextmanager
    def _fake_launch_page(base_url: str):
        yield _FakePage()

    def _fake_replay(artifact, inputs, surface, mode, *, evidence=None, **_kwargs):
        return Success(outputs={"balance": "4218.60"}, steps_run=["s1", "s2"],
                       evidence_ref=evidence.evidence_ref())

    monkeypatch.setattr(cli_module, "launch_page", _fake_launch_page)
    monkeypatch.setattr(cli_module, "run_replay", _fake_replay)
    evidence_root = tmp_path / "evidence_out"
    result = runner.invoke(app, [
        "replay", artifact.id, str(artifact.version), "--root", str(tmp_path),
        "--base-url", "http://127.0.0.1:1", "--policy", _policy_file(tmp_path),
        "--evidence-root", str(evidence_root), "--input", "member_id=12345",
    ])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["outputs"] == {"balance": "4218.60"}
    run_dir = evidence_root / payload["evidence_ref"]
    assert json.loads((run_dir / "result.json").read_text())["outputs"] == {"balance": "**18.60"}


# --- Criterion 8: no credential anywhere in the evidence directory after a login-bearing run

def test_no_credential_appears_anywhere_in_the_evidence_of_a_login_bearing_run(
    tmp_path, live_mockapp,
) -> None:
    # Real browser, real app, the CLI's own `launch_page` (this module holds no `browser`
    # fixture -- one sync driver per thread). The password is a `sensitive` input (E11).
    # The run is *made to fail on the password step* -- an expect nothing on the login page
    # satisfies -- so the engine captures a frame while the credential is typed into the
    # field. Chromium's aria snapshot renders that field as `textbox "Password":
    # teller-demo-pw` and its row as `row "Password teller-demo-pw"` (live-snapshots.txt,
    # "LOGIN page snapshot"), so this is the one construction that puts the credential in
    # front of every layer at once: the snapshot scrub, the `Failure.observed` text built
    # from scrubbed node names, `run.json`'s input mask, stdout. Then every file under the
    # evidence directory is read and grepped.
    from mockapp.app import DEFAULT_LOGIN_PASSWORD, DEFAULT_LOGIN_USER

    top = [SurfaceSegment(kind="window", name="main")]

    def _loc(role: str, name: str) -> Locator:
        return Locator(role=role, name=name, surface_path=top, rationale="login form",
                       confidence="high")

    artifact = base(
        inputs={"user": InputSpec(type="string", required=True),
                "password": InputSpec(type="string", required=True, sensitive=True)},
        outputs={},
        settle=Settle(timeout_ms=1500, poll_ms=200),
        steps=[
            Step(id="s1", action="navigate", target=Target(path="/login"), risk="safe"),
            Step(id="s2", action="fill", locator=_loc("textbox", "User"),
                 value={"from_input": "user"}, risk="safe"),
            Step(id="s3", action="fill", locator=_loc("textbox", "Password"),
                 value={"from_input": "password"}, risk="safe",
                 expects=[Expect(when=Matcher(role="button", name="Never on this page"),
                                 outcome="continue", source="observed")]),
        ],
        success=ArtifactSuccess(checkpoint=Matcher(role="button", name="Search")),
    )
    save(artifact, tmp_path)
    evidence_root = tmp_path / "evidence_out"
    result = runner.invoke(app, [
        "replay", artifact.id, str(artifact.version), "--root", str(tmp_path),
        "--base-url", live_mockapp, "--policy", _policy_file(tmp_path, live_mockapp),
        "--evidence-root", str(evidence_root),
        "--input", f"user={DEFAULT_LOGIN_USER}", "--input", f"password={DEFAULT_LOGIN_PASSWORD}",
    ])
    payload = json.loads(result.stdout)
    assert result.exit_code == 1, payload
    assert payload["kind"] == "NO_BRANCH_MATCHED" and payload["step_id"] == "s3"
    assert DEFAULT_LOGIN_PASSWORD not in result.stdout
    written = {
        str(p.relative_to(evidence_root)): p.read_bytes()
        for p in evidence_root.rglob("*") if p.is_file()
    }
    for expected in ("trace.jsonl", "run.json", "result.json", "artifact.yaml",
                     "snapshots/s3.yaml", "screenshots/s3.png"):
        assert any(name.endswith(expected) for name in written), (expected, sorted(written))
    for name, blob in written.items():
        assert DEFAULT_LOGIN_PASSWORD.encode() not in blob, name
    run_json = json.loads(next(b for n, b in written.items() if n.endswith("run.json")))
    assert run_json["inputs"] == {"user": DEFAULT_LOGIN_USER, "password": "[REDACTED]"}
    snapshot = next(b for n, b in written.items() if n.endswith("snapshots/s3.yaml")).decode()
    assert "[REDACTED]" in snapshot and DEFAULT_LOGIN_USER in snapshot
    trace = next(b for n, b in written.items() if n.endswith("trace.jsonl")).decode()
    assert '"kind": "step_started"' in trace and '"step_id": "s3"' in trace
