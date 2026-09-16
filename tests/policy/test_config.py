"""Spec §6.1: the allowlist is configuration, never the artifact. `PolicyConfig` is
`DeploymentAllowlist` extended (E1), loaded from YAML.
"""
from pathlib import Path

import pytest

from cua.artifact.validate import DeploymentAllowlist
from cua.policy.config import PolicyConfig, load_policy


def test_the_example_policy_file_loads_and_is_a_deployment_allowlist() -> None:
    policy = load_policy(Path("policy.example.yaml"))
    assert isinstance(policy, DeploymentAllowlist)
    assert policy.policy_mode == "strict"
    assert "/account/close" in policy.denied_paths
    assert policy.permits_path("/account/close?number=1") is False
    assert set(policy.allowed_actions) == {
        "navigate", "click", "fill", "select", "press_key", "wait_for", "read", "dismiss_dialog",
    }


def test_a_missing_file_is_file_not_found(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_policy(tmp_path / "nope.yaml")


def test_an_unknown_key_is_refused(tmp_path) -> None:
    path = tmp_path / "p.yaml"
    path.write_text("allowed_origins: []\nallowed_paths: ['/']\nallow_paths: ['/']\n")
    with pytest.raises(ValueError, match="allow_paths"):
        load_policy(path)


def test_a_non_mapping_document_is_refused(tmp_path) -> None:
    path = tmp_path / "p.yaml"
    path.write_text("- just\n- a list\n")
    with pytest.raises(ValueError, match="mapping"):
        load_policy(path)


def test_policy_mode_is_a_closed_literal(tmp_path) -> None:
    path = tmp_path / "p.yaml"
    path.write_text("policy_mode: yolo\nallowed_paths: ['/']\n")
    with pytest.raises(ValueError, match="policy_mode"):
        load_policy(path)


def test_narrowing_a_policy_config_keeps_its_type_and_mode() -> None:
    from cua.artifact.models import CapabilityPolicy
    policy = PolicyConfig(policy_mode="sandbox", allowed_origins=["http://h"],
                          allowed_paths=["/"], allowed_actions=["read"])
    effective = policy.narrowed_by(CapabilityPolicy(allowed_paths=["/m/"]))
    assert isinstance(effective, PolicyConfig)
    assert effective.policy_mode == "sandbox"
    assert effective.allowed_paths == ["/m/"]
