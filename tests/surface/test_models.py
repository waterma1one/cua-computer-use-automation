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


def test_assigning_a_value_onto_a_protected_node_raises() -> None:
    node = Node(
        index=0, role="textbox", name="PIN", value=None,
        state=NodeState(protected=True), surface_path=[seg("window", "main")],
    )
    with pytest.raises(ValidationError):
        node.value = "1234"


def test_marking_a_valued_node_protected_after_the_fact_raises() -> None:
    node = Node(
        index=0, role="textbox", name="PIN", value="1234",
        state=NodeState(protected=False), surface_path=[seg("window", "main")],
    )
    with pytest.raises(ValidationError):
        node.state = NodeState(protected=True)


def test_role_name_locator_requires_a_role() -> None:
    with pytest.raises(ValidationError):
        Locator(
            strategy="role_name", name="Search",
            surface_path=[seg("window", "main")],
            rationale="x", confidence="high",
        )


def test_text_locator_does_not_require_a_role() -> None:
    loc = Locator(
        strategy="text",
        surface_path=[seg("window", "main")],
        rationale="Matches the outcome banner by its visible text.", confidence="high",
    )
    assert loc.role is None
