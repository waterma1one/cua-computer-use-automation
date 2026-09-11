import pytest
from pydantic import ValidationError

from cua.artifact import validate
from cua.surface import snapshot
from cua.surface.models import (
    Locator,
    Node,
    NodeState,
    Observation,
    SurfaceSegment,
    is_protected_name,
)


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


def test_a_fallback_in_a_different_surface_path_is_rejected() -> None:
    # R18: a fallback must carry the same surface_path as the locator it backs, or the
    # recovery path reopens the exact cross-frame ambiguity R8 closes on the primary path.
    fallback = Locator(
        role="button", name="Select",
        surface_path=[seg("window", "main"), seg("frame", "other")],
        rationale="a fallback living in a different frame", confidence="high",
    )
    with pytest.raises(ValidationError):
        Locator(
            role="button", name="Select",
            surface_path=[seg("window", "main"), seg("frame", "content")],
            fallbacks=[fallback],
            rationale="x", confidence="high",
        )


# I8 / R26: the seam must not carry a browser-only word. A desktop surface's path is
# window then pane (spec §3.3); before this, `"pane"` was not a legal `SurfaceSegment.kind`
# at all, so a desktop implementer could not satisfy the vocabulary without editing this
# module.
def test_surface_segment_accepts_a_pane_kind_for_desktop_surfaces() -> None:
    segment = SurfaceSegment(kind="pane", name="main")
    assert segment.kind == "pane"


# Also fix: a negative ordinal is exactly the DOM-ish positional index spec §3.4 rule 1
# outlaws, and it behaved inconsistently besides (-2 resolved via Python's negative-slice
# semantics; -1 always came back not_found because `matches[-1:0]` is empty). Constrained
# to >= 0 at construction, not merely at resolution time.
def test_ordinal_rejects_a_negative_value() -> None:
    with pytest.raises(ValidationError):
        Locator(
            role="button", name="Select", ordinal=-1,
            surface_path=[seg("window", "main")],
            rationale="x", confidence="high",
        )


# E6/R22: the credential-name rule is security-relevant and is needed in two layers -- the
# aria-snapshot parser infers `Node.state.protected` from it, and `cua.artifact.validate`
# uses it for spec S4.4's sixth condition. Two copies of a rule like this drift, and a drift
# is a credential leaking past one of the two checks that exist to stop it, so there is
# exactly one implementation and these tests pin its behaviour at the shared site.
def test_is_protected_name_matches_a_credential_token_case_insensitively() -> None:
    assert is_protected_name("Password")
    assert is_protected_name("teller passwd")
    assert is_protected_name("CVV")


def test_is_protected_name_matches_on_word_boundaries_not_as_a_substring() -> None:
    # A naive substring match on "pin" makes "Shipping" and "Spinner" credential fields.
    assert not is_protected_name("Shipping Address")
    assert not is_protected_name("Spinner")


def test_is_protected_name_treats_a_missing_name_as_unprotected() -> None:
    # The documented blind spot: a password field with no accessible name at all cannot be
    # detected from its name, because there is no name to look at.
    assert not is_protected_name(None)
    assert not is_protected_name("")


def test_is_protected_name_misses_plurals_and_compounds() -> None:
    # The third documented blind spot (M9), pinned so the next reader meets it on purpose
    # rather than assuming coverage. A persisted locator name is likelier to carry a plural
    # than a live node's label is.
    assert not is_protected_name("Passwords")
    assert not is_protected_name("PINs")
    assert not is_protected_name("password_field")
    assert not is_protected_name("MyPassword")


def test_the_protected_name_rule_has_exactly_one_implementation() -> None:
    # The drift guard for E6. Before this, `snapshot.py` and `validate.py` each compiled a
    # byte-identical regex over the shared tuple and nothing held them equal. Both now route
    # through `is_protected_name`; a module-private copy reappearing in either is the defect
    # this test exists to catch.
    assert not hasattr(snapshot, "_PROTECTED_NAME_RE")
    assert not hasattr(validate, "_PROTECTED_NAME_RE")
    assert not hasattr(snapshot, "_is_protected")
    assert snapshot.is_protected_name is is_protected_name
    assert validate.is_protected_name is is_protected_name
