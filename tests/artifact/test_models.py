import pytest
from pydantic import ValidationError

from cua.artifact.models import (
    Artifact,
    Expect,
    FromInput,
    FromStep,
    InputSpec,
    LiteralValue,
    Matcher,
    OutputSpec,
    Step,
    Target,
)


def test_a_matcher_needs_no_rationale_or_surface_path() -> None:
    # E1: a `when` clause is a predicate over what is on screen, not an instruction to act on
    # a control, so it deliberately does not carry Locator's resolution apparatus.
    m = Matcher(role="heading", name="Member Search")
    assert m.strategy == "role_name"
    assert m.name_match == "exact"


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
