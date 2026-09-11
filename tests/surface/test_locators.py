from cua.surface.locators import resolve_against, synthesize
from cua.surface.models import Locator, Node, NodeState, SurfaceSegment

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


def test_text_strategy_round_trips_for_an_unnamed_control_with_a_value() -> None:
    # CRITICAL 1: an unnamed node with a distinct value (phase 1's pinned unnamed-input
    # hostile case) must synthesize a working text-strategy locator, not crash.
    nodes = [
        Node(index=0, role="textbox", name=None, value="alpha", depth=0,
             state=NodeState(), surface_path=PATH),
        Node(index=1, role="textbox", name=None, value="beta", depth=0,
             state=NodeState(), surface_path=PATH),
    ]
    loc = synthesize(nodes[0], nodes)
    assert loc.strategy == "text"
    result = resolve_against(loc, nodes)
    assert result.kind == "unique"
    assert result.node.index == 0


def test_ordinal_locator_round_trips_to_the_correct_node() -> None:
    nodes = [node(0, "button", "Select"), node(1, "button", "Select")]
    loc = synthesize(nodes[1], nodes)
    assert loc.scope is None
    assert loc.ordinal == 1
    result = resolve_against(loc, nodes)
    assert result.kind == "unique"
    assert result.node.index == 1


def test_ambiguous_primary_tries_fallbacks_before_reporting_ambiguous() -> None:
    # CRITICAL 2: spec §3.4 rule 3 -- ambiguous tries scope, then ordinal, then fallbacks,
    # then fails hard. Scope/ordinal are already baked in at synthesis time; a fallback
    # that resolves uniquely must be tried before giving up with `ambiguous`.
    nodes = [
        node(0, "button", "Select"),
        node(1, "button", "Select"),
        node(2, "button", "Unique Target"),
    ]
    primary = Locator(
        strategy="role_name", role="button", name="Select", surface_path=PATH,
        rationale="ambiguous on purpose", confidence="low",
    )
    fallback = Locator(
        strategy="role_name", role="button", name="Unique Target", surface_path=PATH,
        rationale="the fallback that should be tried", confidence="high",
    )
    loc = primary.model_copy(update={"fallbacks": [fallback]})
    result = resolve_against(loc, nodes)
    assert result.kind == "unique"
    assert result.node.index == 2


def test_a_disabled_control_found_via_fallback_is_not_reported_as_not_found() -> None:
    # CRITICAL 3: a fallback resolving to a real-but-disabled control must surface as
    # precondition_failed, not be collapsed into a generic not_found.
    nodes = [node(0, "button", "Post Transfer", disabled=True)]
    primary = Locator(
        strategy="role_name", role="button", name="Nonexistent", surface_path=PATH,
        rationale="matches nothing on purpose", confidence="low",
    )
    fallback = Locator(
        strategy="role_name", role="button", name="Post Transfer", surface_path=PATH,
        rationale="the fallback that finds the real, disabled control", confidence="high",
    )
    loc = primary.model_copy(update={"fallbacks": [fallback]})
    result = resolve_against(loc, nodes)
    assert result.kind == "precondition_failed"
    assert result.which == "enabled"


def test_a_later_fallback_can_still_resolve_uniquely_after_an_earlier_one_is_ambiguous() -> None:
    # Fix round 2: the fallback chain must be exhausted looking for a unique result before
    # settling for the first non-not_found result. A primary that is not_found, followed by
    # an ambiguous fallback and then a fallback that resolves uniquely, must still return
    # the unique result -- the ambiguous fallback must not short-circuit the search.
    nodes = [
        node(0, "button", "Select"),
        node(1, "button", "Select"),
        node(2, "button", "Unique Target"),
    ]
    primary = Locator(
        strategy="role_name", role="button", name="Nonexistent", surface_path=PATH,
        rationale="matches nothing on purpose", confidence="low",
    )
    ambiguous_fb = Locator(
        strategy="role_name", role="button", name="Select", surface_path=PATH,
        rationale="ambiguous, tried first", confidence="low",
    )
    unique_fb = Locator(
        strategy="role_name", role="button", name="Unique Target", surface_path=PATH,
        rationale="should still be tried after the ambiguous fallback", confidence="high",
    )
    loc = primary.model_copy(update={"fallbacks": [ambiguous_fb, unique_fb]})
    result = resolve_against(loc, nodes)
    assert result.kind == "unique"
    assert result.node.index == 2


def test_the_first_non_unique_result_in_attempt_order_is_reported_when_nothing_resolves() -> None:
    # When no attempt (primary or any fallback) resolves uniquely, the reported result is
    # the first non-not_found result in attempt order -- primary, then fallbacks in their
    # given order -- not a severity ranking between ambiguous and precondition_failed.
    nodes = [
        node(0, "button", "Select"),
        node(1, "button", "Select"),
        node(2, "button", "Post Transfer", disabled=True),
    ]
    primary = Locator(
        strategy="role_name", role="button", name="Nonexistent", surface_path=PATH,
        rationale="matches nothing on purpose", confidence="low",
    )
    ambiguous_fb = Locator(
        strategy="role_name", role="button", name="Select", surface_path=PATH,
        rationale="ambiguous, tried first", confidence="low",
    )
    disabled_fb = Locator(
        strategy="role_name", role="button", name="Post Transfer", surface_path=PATH,
        rationale="found but disabled, tried second", confidence="high",
    )
    loc = primary.model_copy(update={"fallbacks": [ambiguous_fb, disabled_fb]})
    result = resolve_against(loc, nodes)
    assert result.kind == "ambiguous"
    assert result.count == 2


# I9: name_match semantics were entirely untested -- making "exact" behave as "contains"
# left the whole suite green. These three pin each mode against a case the other two modes
# would answer differently.
def test_name_match_exact_requires_the_full_name() -> None:
    nodes = [node(0, "button", "Search Members")]
    loc = Locator(
        strategy="role_name", role="button", name="Search", name_match="exact",
        surface_path=PATH, rationale="x", confidence="high",
    )
    result = resolve_against(loc, nodes)
    assert result.kind == "not_found"


def test_name_match_contains_matches_a_substring_anywhere() -> None:
    nodes = [node(0, "button", "Search Members")]
    loc = Locator(
        strategy="role_name", role="button", name="Members", name_match="contains",
        surface_path=PATH, rationale="x", confidence="high",
    )
    result = resolve_against(loc, nodes)
    assert result.kind == "unique"
    assert result.node.index == 0


def test_name_match_prefix_matches_only_at_the_start() -> None:
    nodes = [node(0, "button", "Search Members"), node(1, "button", "Member Search")]
    loc = Locator(
        strategy="role_name", role="button", name="Search", name_match="prefix",
        surface_path=PATH, rationale="x", confidence="high",
    )
    result = resolve_against(loc, nodes)
    assert result.kind == "unique"
    assert result.node.index == 0


# I2: an unnamed intermediate container (a bare rowgroup wrapper, name="") sitting between
# the target and its correct named scope must not stop the ancestor walk or push synthesis
# down to `ordinal` -- it must be skipped, and the walk must keep climbing to the named row
# above it. This is the pure-layer half of I2; the web-surface half is that `act_on_index`
# must run this same reasoning against the raw, unfiltered node population rather than the
# filtered-and-truncated one the model was shown (see tests/surface/test_web_surface.py).
def test_scope_synthesis_climbs_past_an_unnamed_intermediate_ancestor() -> None:
    nodes = [
        node(0, "rowgroup", "", depth=0),
        node(1, "row", "Savings 000100045512-01 4,218.60 Select", depth=1),
        node(2, "button", "Select", depth=2),
        node(3, "rowgroup", "", depth=0),
        node(4, "row", "Checking 000100045512-02 312.04 Select", depth=1),
        node(5, "button", "Select", depth=2),
    ]
    loc = synthesize(nodes[2], nodes)
    assert loc.scope is not None, "an unnamed intermediate ancestor must not defeat scoping"
    assert "Savings" in (loc.scope.name or "")
    assert loc.ordinal is None


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


# E8: a cyclic fallback chain is constructible with plain Pydantic attribute assignment --
# no `model_construct` or `model_copy` bypass -- so `resolve_against` recursing through
# `fallbacks` reached `RecursionError`. Phase 2 triaged this as deferred partly on the
# belief that building one required a bypass; it does not, phase 3's compiler is the first
# real producer of `fallbacks`, and phase 4 replays fallback chains.
def test_resolve_against_terminates_on_a_cyclic_fallback_chain() -> None:
    first = Locator(role="button", name="Select", surface_path=PATH,
                    rationale="primary", confidence="high")
    second = Locator(role="button", name="Choose", surface_path=PATH,
                     rationale="fallback", confidence="low")
    first.fallbacks = [second]
    second.fallbacks = [first]

    result = resolve_against(first, [node(0, "button", "Choose")])

    # It must terminate *and* still reach the fallback: a guard that bailed out of the
    # whole walk would return not_found here, which is a silent resolution failure -- the
    # exact class `require` and `ambiguous` exist to surface.
    assert result.kind == "unique"
    assert result.node.name == "Choose"


def test_a_self_referencing_fallback_terminates() -> None:
    loc = Locator(role="button", name="Select", surface_path=PATH,
                  rationale="primary", confidence="high")
    loc.fallbacks = [loc]
    assert resolve_against(loc, []).kind == "not_found"
