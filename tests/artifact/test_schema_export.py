"""Spec §4.2 decision 5: the artifact is already a tool definition, and the catalog
exports it verbatim -- built from the declared `inputs`/`outputs`, never from
`Artifact.model_json_schema()`, which would export the artifact's own shape instead of
this capability's argument contract.
"""

from __future__ import annotations

import jsonschema

from cua.artifact.models import InputSpec, OutputSpec
from cua.artifact.schema import export_tool_schema
from tests.artifact.factories import base

# E14: the mechanic vocabulary the export must never carry as a *key*, anywhere in the
# exported tree. Checked by exact key membership, not substring, so a legitimate input
# named `locator_hint` or a description that happens to say "steps" does not trip it --
# see test_the_leak_check_does_not_fire_on_legitimate_content_naming_a_mechanic below for
# why that distinction matters.
FORBIDDEN_MECHANIC_KEYS = {
    "locator", "surface_path", "rationale", "confidence", "steps", "frame",
    "provenance", "recovery", "success",
}


def _all_keys(obj: object) -> set[str]:
    """Recursively collect every dict key anywhere inside `obj`."""
    keys: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            keys.add(key)
            keys |= _all_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            keys |= _all_keys(item)
    return keys


def test_the_export_is_itself_valid_json_schema() -> None:
    schema = export_tool_schema(base())
    jsonschema.Draft202012Validator.check_schema(schema["input_schema"])


def test_declared_inputs_round_trip_into_the_schema() -> None:
    a = base(inputs={"member_id": InputSpec(type="string", pattern="^[0-9]{5}$", required=True)})
    props = export_tool_schema(a)["input_schema"]["properties"]
    assert props["member_id"]["type"] == "string"
    assert props["member_id"]["pattern"] == "^[0-9]{5}$"


def test_required_inputs_are_marked_required_and_optional_ones_are_not() -> None:
    a = base(inputs={"member_id": InputSpec(type="string", required=True),
                     "note": InputSpec(type="string", required=False)})
    assert export_tool_schema(a)["input_schema"]["required"] == ["member_id"]


def test_an_argument_matching_the_schema_validates_and_a_bad_one_does_not() -> None:
    a = base(inputs={"member_id": InputSpec(type="string", pattern="^[0-9]{5}$", required=True)})
    schema = export_tool_schema(a)["input_schema"]
    jsonschema.validate({"member_id": "12345"}, schema)
    try:
        jsonschema.validate({"member_id": "nope"}, schema)
    except jsonschema.ValidationError:
        pass
    else:
        raise AssertionError("the pattern was exported but is not being enforced")


def test_the_export_carries_the_name_and_description_a_calling_agent_needs() -> None:
    schema = export_tool_schema(base())
    assert schema["name"] == "probe"
    assert schema["description"]


def test_the_export_leaks_no_locator_or_step_detail() -> None:
    # §4.2 decision 5 / the no-mechanics rule: what a caller gets is the contract, never
    # the mechanics. E14 rescoped this from a substring scan over a stringified blob to
    # the exported *structure*: the top-level key set is exactly the contract's shape, and
    # no key anywhere in the tree names a mechanic. A free-text scan fires on legitimate
    # content that merely mentions a mechanic word (see the two tests below), which is
    # exactly the failure mode phase 2's `scrub_protected_values` substring scan already
    # paid for once -- and a guard that fires on legitimate content is the guard the next
    # person softens, which is how the real guarantee dies.
    schema = export_tool_schema(base())
    assert set(schema.keys()) == {"name", "description", "input_schema", "output_schema"}
    assert _all_keys(schema).isdisjoint(FORBIDDEN_MECHANIC_KEYS)


def test_the_leak_check_still_catches_an_actual_mechanic_leak() -> None:
    # Proves the structural check above is strictly stronger than "different": it must
    # still fire on a real leak, not just stop firing on false positives.
    schema = export_tool_schema(base())
    schema["input_schema"]["properties"]["member_id"]["locator"] = {"role": "button"}
    assert not _all_keys(schema).isdisjoint(FORBIDDEN_MECHANIC_KEYS)


def test_the_leak_check_does_not_fire_on_legitimate_content_naming_a_mechanic() -> None:
    # The exact false-positive the old substring scan was vulnerable to: a declared input
    # literally named `locator_hint`, and a description that uses the ordinary English
    # word "steps", are both legitimate contract content -- neither is a mechanic leak.
    a = base(
        inputs={"locator_hint": InputSpec(type="string", required=False)},
        description="Reads the balance in two steps.",
    )
    schema = export_tool_schema(a)
    assert set(schema.keys()) == {"name", "description", "input_schema", "output_schema"}
    assert _all_keys(schema).isdisjoint(FORBIDDEN_MECHANIC_KEYS)


def test_the_schema_keyword_is_pinned_to_draft_2020_12() -> None:
    # Minor from fix round 1: dropping `$schema` entirely left every test green, so its
    # value must be pinned explicitly rather than left merely-present.
    schema = export_tool_schema(base())
    assert schema["input_schema"]["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["output_schema"]["$schema"] == "https://json-schema.org/draft/2020-12/schema"


# E13: a sensitive input or a redacted output must be flagged in the export, and an
# ordinary one must not carry the flag at all -- both directions, per the phase's
# standing instruction.


def test_a_sensitive_input_is_flagged_in_the_exported_property() -> None:
    a = base(inputs={"pin": InputSpec(type="string", required=True, sensitive=True)})
    props = export_tool_schema(a)["input_schema"]["properties"]
    assert props["pin"]["sensitive"] is True


def test_a_non_sensitive_input_carries_no_sensitive_key() -> None:
    a = base(inputs={"note": InputSpec(type="string", required=False)})
    props = export_tool_schema(a)["input_schema"]["properties"]
    assert "sensitive" not in props["note"]


def test_a_redacted_output_is_flagged_in_the_exported_property() -> None:
    a = base(outputs={"ssn": OutputSpec(type="string", redact=True)})
    props = export_tool_schema(a)["output_schema"]["properties"]
    assert props["ssn"]["redact"] is True


def test_a_non_redacted_output_carries_no_redact_key() -> None:
    a = base(outputs={"balance": OutputSpec(type="string")})
    props = export_tool_schema(a)["output_schema"]["properties"]
    assert "redact" not in props["balance"]


def test_a_sensitive_flag_does_not_break_json_schema_validity() -> None:
    # `sensitive`/`redact` are unrecognised JSON Schema keywords by design (no standard
    # keyword covers them) -- confirm that stays inert rather than breaking the schema or
    # the values it accepts.
    a = base(inputs={"pin": InputSpec(type="string", required=True, sensitive=True)})
    schema = export_tool_schema(a)["input_schema"]
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate({"pin": "1234"}, schema)


# The brief's checks above are one-directional in a couple of places the phase's other
# tasks got burned by (ORDINAL_USED unpinned in the over-firing direction, Step.risk's
# default unpinned, three of four OVERLAY_UNKNOWN_STEP branches deletable with the suite
# green). Pin the opposite case for each one this module asserts.


def test_an_optional_input_is_not_forced_required() -> None:
    # Opposite of test_required_inputs_are_marked_required_and_optional_ones_are_not:
    # confirms `required` isn't just always empty or always everything.
    a = base(inputs={"note": InputSpec(type="string", required=False)})
    assert export_tool_schema(a)["input_schema"]["required"] == []


def test_no_pattern_declared_means_no_pattern_key_exported() -> None:
    # Opposite of test_declared_inputs_round_trip_into_the_schema: a pattern must not be
    # fabricated for an input that declares none.
    a = base(inputs={"member_id": InputSpec(type="string", required=True)})
    props = export_tool_schema(a)["input_schema"]["properties"]
    assert "pattern" not in props["member_id"]


def test_declared_outputs_round_trip_into_the_output_schema() -> None:
    a = base(outputs={"balance": OutputSpec(type="string", format="date")})
    props = export_tool_schema(a)["output_schema"]["properties"]
    assert props["balance"]["type"] == "string"
    assert props["balance"]["format"] == "date"


def test_an_argument_without_the_pattern_field_is_not_over_constrained() -> None:
    # Opposite of test_an_argument_matching_the_schema_validates_and_a_bad_one_does_not:
    # an input with no declared pattern must accept an arbitrary string, not silently
    # inherit some other input's constraint or reject on shape alone.
    a = base(inputs={"note": InputSpec(type="string", required=False)})
    schema = export_tool_schema(a)["input_schema"]
    jsonschema.validate({"note": "anything at all, no digits required"}, schema)
