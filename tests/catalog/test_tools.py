from cua.artifact.schema import export_tool_schema
from cua.artifact.store import has_irreversible_step
from cua.catalog.tools import ToolDefinition, build_tool_definition
from tests.artifact.factories import base


def test_the_tool_definition_wraps_the_exported_schema_unmodified() -> None:
    artifact = base()
    schema = export_tool_schema(artifact)
    tool = build_tool_definition(artifact, schema, status="draft", allowlist_checked=False)
    assert isinstance(tool, ToolDefinition)
    assert tool.schema is schema


def test_a_tool_definition_advertises_an_irreversible_step() -> None:
    artifact = base()
    assert not has_irreversible_step(artifact)
    assert build_tool_definition(
        artifact, export_tool_schema(artifact), status="draft", allowlist_checked=False
    ).irreversible is False
    artifact.steps[0].risk = "irreversible"
    assert has_irreversible_step(artifact)
    assert build_tool_definition(
        artifact, export_tool_schema(artifact), status="draft", allowlist_checked=False
    ).irreversible is True
