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
    # §4.2 decision 5: the artifact is already a tool definition, and the catalog exports it
    # verbatim. What a caller gets is the contract, never the mechanics.
    blob = repr(export_tool_schema(base())).lower()
    for forbidden in ("locator", "surface_path", "rationale", "confidence", "steps", "frame"):
        assert forbidden not in blob


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
