from typing import Any

import pytest
from pydantic import ValidationError

from cua.artifact.models import (
    App,
    Artifact,
    CapabilityPolicy,
    Expect,
    FromInput,
    FromStep,
    InputSpec,
    LiteralValue,
    Matcher,
    OutputSpec,
    Provenance,
    Settle,
    Step,
    Success,
    Target,
)


def _minimal_artifact_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        schema_version=1,
        id="corebank.member_savings_balance",
        version=1,
        name="lookup_member_savings_balance",
        description="Look up a member by ID and return their current savings balance.",
        verified=False,
        app=App(vendor_product="corebank-teller", variant="base", surface="web",
                 entry="/teller/index.html"),
        settle=Settle(timeout_ms=8000, poll_ms=200),
        max_duration_ms=120000,
        steps=[],
        success=Success(checkpoint=Matcher(role="heading", name="x")),
        provenance=Provenance(
            discovered_at="2026-09-09T00:00:00",
            model="gemini-2.5-flash-lite",
            policy_mode="sandbox",
            provider_retention="training_permitted",
            run_id="r_01H",
            trace_ref="evidence/r_01H/trace.jsonl",
        ),
    )
    base.update(overrides)
    return base


def test_a_matcher_needs_no_rationale_or_surface_path() -> None:
    # E1: a `when` clause is a predicate over what is on screen, not an instruction to act on
    # a control, so it deliberately does not carry Locator's resolution apparatus. Pinning the
    # exact field set, not just two defaults, so a future widening toward Locator's shape
    # (surface_path, rationale, confidence) cannot creep in unnoticed.
    m = Matcher(role="heading", name="Member Search")
    assert m.strategy == "role_name"
    assert m.name_match == "exact"
    assert set(Matcher.model_fields) == {"strategy", "role", "name", "name_match"}


def test_a_matcher_rejects_a_regular_expression_name_match() -> None:
    with pytest.raises(ValidationError):
        Matcher(role="heading", name="x", name_match="regex")


def test_a_step_value_is_discriminated_by_which_key_is_present() -> None:
    from_input = Step(id="s2", action="fill", value={"from_input": "member_id"})
    literal = Step(id="s2", action="fill", value={"literal": "12345"})
    from_step = Step(id="s2", action="fill", value={"from_step": "s1"})
    assert isinstance(from_input.value, FromInput)
    assert isinstance(literal.value, LiteralValue)
    assert isinstance(from_step.value, FromStep)


def test_a_step_value_carrying_two_keys_is_rejected_rather_than_guessed() -> None:
    # E3: presence is the discriminator, so an ambiguous mapping must fail loudly instead of
    # resolving to whichever arm happened to match first.
    with pytest.raises(ValidationError):
        Step(id="s2", action="fill", value={"from_input": "member_id", "literal": "12345"})


def test_a_navigate_step_carries_a_path_and_no_host() -> None:
    # E2, and §4.2 decision 4: an artifact carrying a host would be tenant-locked by construction.
    step = Step(id="s1", action="navigate", target=Target(path="/teller/index.html"), risk="safe")
    assert step.target is not None and step.target.path.startswith("/")
    assert not hasattr(step.target, "host")


def test_an_unverified_expect_defaults_to_unverified() -> None:
    # §8.3 step 5: the compiler cannot invent knowledge of states it never saw.
    proposed = Expect(when=Matcher(role="heading", name="x"), outcome="continue", source="proposed")
    assert proposed.verified is False


def test_an_observed_expect_may_be_verified() -> None:
    observed = Expect(
        when=Matcher(role="heading", name="x"), outcome="continue",
        source="observed", verified=True,
    )
    assert observed.verified is True


def test_a_proposed_expect_cannot_claim_to_be_verified() -> None:
    with pytest.raises(ValidationError):
        Expect(
            when=Matcher(role="heading", name="x"), outcome="continue",
            source="proposed", verified=True,
        )


def test_an_artifact_has_no_lifecycle_state() -> None:
    # §4.1: `status` and `stability` live in artifacts/registry.json, keyed by (id, version).
    # The artifact file is immutable, so lifecycle state cannot live in it.
    assert "status" not in Artifact.model_fields
    assert "stability" not in Artifact.model_fields


def test_schema_version_and_version_are_separate_fields() -> None:
    # §4.2 decision 3: one governs parsing, the other the capability revision.
    assert "schema_version" in Artifact.model_fields
    assert "version" in Artifact.model_fields


def test_an_input_may_be_marked_sensitive_and_an_output_redacted() -> None:
    # §4.2 decision 6: redaction is declared by the author, never inferred by guessing at PII.
    assert InputSpec(type="string", required=True, sensitive=True).sensitive is True
    assert OutputSpec(type="string", redact=True).redact is True


def test_target_rejects_an_unknown_key() -> None:
    # E5: the artifact layer fails loud on an unrecognized key rather than silently dropping
    # it, because this file is authored and hand-edited by a human. `host` is the case E2
    # exists to prevent: a locator or step carrying a host would be tenant-locked by
    # construction, so it must never silently vanish into a parsed-but-ignored field.
    with pytest.raises(ValidationError):
        Target(path="/teller/index.html", host="evil.example.com")  # type: ignore[call-arg]


def test_artifact_distinguishes_no_policy_from_an_empty_policy() -> None:
    # No `policy` key at all must not parse to the same state as an explicit, empty policy
    # block -- Task 2's narrowing check has to tell "not declared" from "declared and empty"
    # apart, and emit a note-level finding only in the former case.
    no_policy = Artifact(**_minimal_artifact_kwargs())
    empty_policy = Artifact(**_minimal_artifact_kwargs(policy=CapabilityPolicy()))
    assert no_policy.policy is None
    assert empty_policy.policy is not None
    assert empty_policy.policy == CapabilityPolicy()


def test_expect_assignment_flipping_a_verified_clause_to_proposed_raises() -> None:
    observed_and_verified = Expect(
        when=Matcher(role="heading", name="x"), outcome="continue",
        source="observed", verified=True,
    )
    with pytest.raises(ValidationError):
        observed_and_verified.source = "proposed"


def test_expect_assignment_flipping_a_proposed_clause_to_verified_raises() -> None:
    proposed = Expect(when=Matcher(role="heading", name="x"), outcome="continue", source="proposed")
    with pytest.raises(ValidationError):
        proposed.verified = True


def test_a_step_built_without_risk_is_unclassified_not_safe() -> None:
    # §6.3's asymmetry: over-classification costs a human a moment at approval time, while a
    # genuine irreversible action classified as safe and replayed unattended is the error that
    # cannot be undone. An omitted `risk` must read back as "not yet classified" (`None`), never
    # default to `"safe"`.
    step = Step(id="s2", action="fill", value={"literal": "12345"})
    assert step.risk is None
