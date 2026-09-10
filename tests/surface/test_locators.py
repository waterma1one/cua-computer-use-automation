from cua.surface.locators import resolve_against, synthesize
from cua.surface.models import Node, NodeState, SurfaceSegment

PATH = [SurfaceSegment(kind="window", name="main"), SurfaceSegment(kind="frame", name="content")]
OTHER_PATH = [
    SurfaceSegment(kind="window", name="main"), SurfaceSegment(kind="frame", name="other")
]


def node(i: int, role: str, name: str, depth: int = 0, **state: bool) -> Node:
    return Node(index=i, role=role, name=name, value=None, depth=depth,
                state=NodeState(**state), surface_path=PATH)


def test_a_uniquely_named_control_needs_no_disambiguation() -> None:
    nodes = [node(0, "textbox", "Member ID"), node(1, "button", "Search")]
    loc = synthesize(nodes[1], nodes)
    assert loc.name == "Search"
    assert loc.scope is None and loc.ordinal is None
    assert loc.confidence == "high"
    assert loc.rationale


def test_duplicate_names_are_disambiguated_by_scope_before_ordinal() -> None:
    # R16: real snapshots never put a row and its button at the same depth -- Task 2's
    # parser emits the row at depth 2 and the button inside it at depth 4. The row names
    # carry the trailing "Select" because a row's accessible name is the concatenation of
    # its cells, and the row does contain a Select button.
    nodes = [
        node(0, "row", "Savings 000100045512-01 4,218.60 Select", depth=0),
        node(1, "button", "Select", depth=1),
        node(2, "row", "Checking 000100045512-02 312.04 Select", depth=0),
        node(3, "button", "Select", depth=1),
    ]
    loc = synthesize(nodes[1], nodes)
    assert loc.scope is not None, "scope must be tried before ordinal"
    assert "Savings" in (loc.scope.name or "")
    assert loc.confidence == "medium"


def test_an_unnamed_control_does_not_produce_a_role_name_locator() -> None:
    nodes = [node(0, "textbox", "")]
    loc = synthesize(nodes[0], nodes)
    assert loc.strategy in ("text", "ax_path")


def test_resolution_reports_ambiguity_rather_than_picking_the_first() -> None:
    nodes = [node(0, "button", "Select"), node(1, "button", "Select")]
    loc = synthesize(nodes[0], nodes)
    loc = loc.model_copy(update={"scope": None, "ordinal": None})
    result = resolve_against(loc, nodes)
    assert result.kind == "ambiguous"
    assert result.count == 2


def test_resolution_fails_the_precondition_for_a_disabled_control() -> None:
    nodes = [node(0, "button", "Post Transfer", disabled=True)]
    loc = synthesize(nodes[0], nodes)
    result = resolve_against(loc, nodes)
    assert result.kind == "precondition_failed"
    assert result.which == "enabled"


def test_cross_frame_duplicate_does_not_cause_ambiguity() -> None:
    # R8: uniqueness is computed among nodes sharing the target's surface_path. A control
    # unique in its own frame must not be scored ambiguous just because an identically
    # named control exists in a different frame.
    nodes = [
        node(0, "button", "Search"),
        Node(index=1, role="button", name="Search", value=None, depth=0,
             state=NodeState(), surface_path=OTHER_PATH),
    ]
    loc = synthesize(nodes[0], nodes)
    assert loc.scope is None and loc.ordinal is None
    assert loc.confidence == "high"


def test_scope_walk_skips_an_ancestor_that_does_not_disambiguate() -> None:
    # On the real member-detail page the nearest named ancestor of a Select button is a
    # cell whose name is also "Select", identical in every row, so it disambiguates
    # nothing -- the row above it is the one that actually works.
    nodes = [
        node(0, "row", "Savings 000100045512-01 4,218.60 Select", depth=2),
        node(1, "cell", "Savings", depth=3),
        node(2, "cell", "4,218.60", depth=3),
        node(3, "cell", "Select", depth=3),
        node(4, "button", "Select", depth=4),
        node(5, "row", "Checking 000100045512-02 312.04 Select", depth=2),
        node(6, "cell", "Checking", depth=3),
        node(7, "cell", "312.04", depth=3),
        node(8, "cell", "Select", depth=3),
        node(9, "button", "Select", depth=4),
    ]
    loc = synthesize(nodes[4], nodes)
    assert loc.scope is not None
    assert loc.scope.role == "row"
    assert "Savings" in (loc.scope.name or "")
    assert loc.confidence == "medium"


def test_resolve_against_a_scoped_locator_with_real_depths() -> None:
    nodes = [
        node(0, "row", "Savings 000100045512-01 4,218.60 Select", depth=2),
        node(1, "cell", "Savings", depth=3),
        node(2, "cell", "4,218.60", depth=3),
        node(3, "cell", "Select", depth=3),
        node(4, "button", "Select", depth=4),
        node(5, "row", "Checking 000100045512-02 312.04 Select", depth=2),
        node(6, "cell", "Checking", depth=3),
        node(7, "cell", "312.04", depth=3),
        node(8, "cell", "Select", depth=3),
        node(9, "button", "Select", depth=4),
    ]
    loc = synthesize(nodes[4], nodes)
    result = resolve_against(loc, nodes)
    assert result.kind == "unique"
    assert result.node.index == 4
