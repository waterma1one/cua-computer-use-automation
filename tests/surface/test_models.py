import pytest
from pydantic import ValidationError

from cua.surface.models import Locator, Node, NodeState, Observation, SurfaceSegment


def seg(kind: str, name: str) -> SurfaceSegment:
    return SurfaceSegment(kind=kind, name=name)


def test_locator_requires_a_rationale() -> None:
    with pytest.raises(ValidationError):
        Locator(role="button", name="Search", surface_path=[seg("window", "main")])


def test_locator_rejects_a_regular_expression_name_match() -> None:
    with pytest.raises(ValidationError):
        Locator(
            role="button", name="Search", name_match="regex",
            surface_path=[seg("window", "main")],
            rationale="x", confidence="high",
        )


def test_locator_serializes_without_any_browser_concept() -> None:
    loc = Locator(
        role="button", name="Search",
        surface_path=[seg("window", "main"), seg("frame", "content")],
        rationale="The only submit control in the search frame.", confidence="high",
    )
    blob = loc.model_dump_json()
    for forbidden in ("css", "xpath", "selector", "nth-child", "querySelector"):
        assert forbidden not in blob.lower()


def test_observation_generations_increase() -> None:
    a = Observation(generation=1, nodes=[], truncated=False)
    b = Observation(generation=2, nodes=[], truncated=False)
    assert b.generation > a.generation


def test_protected_nodes_never_carry_a_value() -> None:
    with pytest.raises(ValidationError):
        Node(
            index=0, role="textbox", name="PIN", value="1234",
            state=NodeState(protected=True), surface_path=[seg("window", "main")],
        )
