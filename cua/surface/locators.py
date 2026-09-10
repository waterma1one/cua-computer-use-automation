"""Synthesizes replayable locators from observed nodes, and resolves locators back to nodes.

Spec §3.4 rule 2 (synthesis) and rule 3 (resolution). This module is where the system's
determinism claim lives: `synthesize` never fabricates a value that would make a locator
prettier but less honest (a role for a strategy that does not need one, a name where there is
none), and `resolve_against` never guesses between multiple matches -- it reports `ambiguous`
and lets a human or a later phase decide, rather than silently clicking the first candidate.

Pure Python plus `cua.surface.models`. No browser library, and no `cua.surface.snapshot` --
nothing here needs a parsed snapshot's parsing logic, only the flat `Node` list it produces.
"""

from __future__ import annotations

from typing import Literal

from cua.surface.models import (
    Ambiguous,
    Locator,
    NameMatch,
    Node,
    NotFound,
    PreconditionFailed,
    Require,
    Resolution,
    Strategy,
    Unique,
)


def ancestors_of(node: Node, nodes: list[Node]) -> list[Node]:
    """Returns `node`'s enclosing nodes within `nodes`, nearest ancestor first.

    The one containment rule `synthesize` and `resolve_against` both use: `nodes[i]`
    encloses `node` when `i` precedes `node`'s position in the list and `nodes[i]` is a
    proper ancestor by depth. Walking backward from `node`'s position, a running minimum
    depth starts at `node.depth`; every preceding node whose depth is strictly below that
    running minimum is an ancestor, and the minimum drops to its depth. A preceding node at
    the same or greater depth is a sibling (or a sibling's descendant) and is skipped
    without breaking the walk, which is what lets the walk reach a grandparent past an
    intervening sibling cell.

    `nodes` should already be filtered to one `surface_path` (as `synthesize` and
    `resolve_against` both do before calling this) -- containment across frames is
    meaningless, since two frames' node lists are simply concatenated, not nested.

    Invariant this relies on and does not check: `nodes` must be in true document
    (pre-order) order -- the order `parse_aria_snapshot` emits, where every node's
    ancestors precede it and depth only rises and falls with actual nesting. A caller that
    passes a reordered or arbitrarily filtered list (sorted by name, shuffled, every third
    node dropped) gets silently wrong ancestors back, not an error: the depth/position walk
    has no way to detect that its input no longer reflects the real tree.
    """
    position = _position_of(node, nodes)
    running_min_depth = node.depth
    ancestors: list[Node] = []
    for candidate in reversed(nodes[:position]):
        if candidate.depth < running_min_depth:
            ancestors.append(candidate)
            running_min_depth = candidate.depth
    return ancestors


def _position_of(node: Node, nodes: list[Node]) -> int:
    for i, candidate in enumerate(nodes):
        if candidate is node:
            return i
    for i, candidate in enumerate(nodes):
        if candidate.index == node.index and candidate.surface_path == node.surface_path:
            return i
    raise ValueError("node is not a member of the given node list")


def _name_matches(candidate: str | None, name: str | None, name_match: NameMatch) -> bool:
    if name is None:
        return candidate is None
    if candidate is None:
        return False
    if name_match == "exact":
        return candidate == name
    if name_match == "contains":
        return name in candidate
    return candidate.startswith(name)  # "prefix"


def _text_of(node: Node) -> str | None:
    """The text a `text`-strategy locator matches against: the accessible name when there
    is one, otherwise the node's literal value. `synthesize` only ever builds a `text`
    locator from `node.value` when `node.name` is already falsy (see `synthesize`), so this
    mirrors that same preference rather than introducing a second rule.
    """
    return node.name if node.name else node.value


def _matches_locator(node: Node, loc: Locator) -> bool:
    """Whether `node` satisfies `loc`'s own match criteria (role/name), ignoring scope.

    `role_name` locators always carry a role (the model enforces it), so the role check
    always applies for them. `text` locators deliberately carry `role=None` (D10: the
    target application carries no ARIA roles for the outcome/recovery text they match), so
    the role check is skipped rather than failing on a role that was never set. `ax_path`
    locators may or may not carry a role, so the same "skip if None" rule covers both.

    `text`-strategy locators match against `_text_of` (name, falling back to value) rather
    than `node.name` alone: a `text` locator synthesized from an unnamed node's `value`
    (phase 1's pinned unnamed-input hostile case) must be able to match that same node
    again, and `node.name` would never equal that value.
    """
    if loc.role is not None and node.role != loc.role:
        return False
    candidate = _text_of(node) if loc.strategy == "text" else node.name
    return _name_matches(candidate, loc.name, loc.name_match)


def _contained_in(node: Node, scope: Locator, nodes: list[Node]) -> bool:
    return any(_matches_locator(ancestor, scope) for ancestor in ancestors_of(node, nodes))


def _synthesize_matching(
    node: Node,
    candidates: list[Node],
    strategy: Strategy,
    role: str | None,
    name: str | None,
    identity: str,
) -> Locator:
    """Runs the uniqueness loop (role/name alone, then scope, then ordinal) for one
    candidate identification (`role`, `name`) of `node`, and returns the resulting locator.

    `identity` is a human-readable description of what is being matched, used only to build
    the rationale sentence -- the actual matching logic is `role`/`name`/`name_match`.
    """
    base = Locator(
        strategy=strategy,
        role=role,
        name=name,
        name_match="exact",
        surface_path=node.surface_path,
        rationale="_",
        confidence="high",
    )
    matches = [n for n in candidates if _matches_locator(n, base)]

    if len(matches) == 1:
        return base.model_copy(update={
            "rationale": f"{identity} is unique on this surface.",
        })

    for ancestor in ancestors_of(node, candidates):
        if not ancestor.name:
            # An ancestor with no accessible name cannot be named in a scope locator
            # without fabricating one -- skip it rather than invent a value for a
            # human-reviewed artifact.
            continue
        scope = Locator(
            strategy="role_name",
            role=ancestor.role,
            name=ancestor.name,
            name_match="exact",
            surface_path=node.surface_path,
            rationale=f"the enclosing {ancestor.role} named '{ancestor.name}'.",
            confidence="high",
        )
        scoped_matches = [n for n in matches if _contained_in(n, scope, candidates)]
        if len(scoped_matches) == 1:
            return base.model_copy(update={
                "scope": scope,
                "confidence": "medium",
                "rationale": (
                    f"{identity} is ambiguous alone ({len(matches)} matches); scoping to "
                    f"the enclosing {ancestor.role} \"{ancestor.name}\" makes it unique."
                ),
            })
        # This ancestor does not disambiguate (every match shares it, or several
        # candidates carry the same ancestor name) -- try the next ancestor out.

    ordinal = _position_of(node, matches)
    return base.model_copy(update={
        "ordinal": ordinal,
        "confidence": "low",
        "rationale": (
            f"{identity} matches {len(matches)} controls and no enclosing ancestor "
            f"disambiguates them; using its ordinal position ({ordinal}) among the matches. "
            "This should be reviewed -- ordinal locators are fragile against reordering."
        ),
    })


def synthesize(node: Node, among: list[Node]) -> Locator:
    """Synthesizes a locator for `node`, unique among the nodes sharing its `surface_path`.

    Spec §3.4 rule 2 / §8.3 step 3: prefer role plus name; add `scope` if that alone is
    ambiguous; add `ordinal` if scope does not resolve it either; fall back to `text` then
    `ax_path` when the node has no name at all. `among` is filtered to `node.surface_path`
    before any counting happens (R8): a control unique in its own frame must not be scored
    ambiguous because of an identically-named control living in a different frame -- the
    emitted locator carries `surface_path` and resolves per-frame regardless.
    """
    candidates = [n for n in among if n.surface_path == node.surface_path]

    if node.name:
        return _synthesize_matching(
            node, candidates, strategy="role_name", role=node.role, name=node.name,
            identity=f"role '{node.role}' with accessible name '{node.name}'",
        )

    if node.value:
        # The node has no accessible name, but does have some literal text (e.g. a typed
        # value) that can stand in for it.
        return _synthesize_matching(
            node, candidates, strategy="text", role=None, name=node.value,
            identity=f"its literal text '{node.value}'",
        )

    # No name and no text at all: identify by accessibility-tree position instead. R17:
    # this is expressed as a nested `scope` chain (built by `_synthesize_matching`'s own
    # ancestor walk if role alone is ambiguous), not as a new model field.
    return _synthesize_matching(
        node, candidates, strategy="ax_path", role=node.role, name=node.name,
        identity=f"role '{node.role}' with no usable name or text",
    )


def _failed_precondition(node: Node, require: Require) -> Literal["visible", "enabled"] | None:
    if require.enabled and node.state.disabled:
        return "enabled"
    # Node carries no visibility flag: the accessibility snapshot a Node is built from
    # already excludes hidden elements, so a node's presence in an Observation at all is
    # itself the visibility signal. There is nothing further to check here.
    return None


def resolve_against(loc: Locator, nodes: list[Node]) -> Resolution:
    """Resolves `loc` against `nodes`, strictly (spec §3.4 rule 3).

    Filters to `loc.surface_path`, matches by strategy, applies `scope` (via the same
    `ancestors_of` containment rule `synthesize` uses) and `ordinal`. Exactly one match is
    checked against `require`; a failing precondition returns `precondition_failed`, never
    `unique` -- a found-but-disabled control resolving as success is a silent-failure class
    this function exists to close off.

    Neither zero matches nor more than one is final until `fallbacks` have been tried, in
    order (spec §3.4 rule 3: "ambiguous tries scope, then ordinal, then fallbacks, then
    fails hard" -- scope and ordinal are already baked into `loc` at synthesis time, so what
    remains at resolution time is the fallback list). A fallback that itself resolves to
    anything other than `not_found` -- `unique`, but also `precondition_failed` or
    `ambiguous` in turn -- is returned as-is rather than collapsed into a generic `not_found`:
    a found-but-disabled control reached through a fallback is exactly the silent-failure
    class `require` exists to catch, and folding it into "not found" would hide it from
    whoever is debugging the replay. Only once every fallback has come back `not_found` (or
    there are none) does this function give up: `ambiguous` if the primary locator itself had
    more than one match, `not_found` otherwise, with a reason naming every fallback that was
    tried and how it failed.
    """
    candidates = [n for n in nodes if n.surface_path == loc.surface_path]
    matches = [n for n in candidates if _matches_locator(n, loc)]

    if loc.scope is not None:
        matches = [n for n in matches if _contained_in(n, loc.scope, candidates)]

    if loc.ordinal is not None:
        matches = matches[loc.ordinal : loc.ordinal + 1]

    if len(matches) == 1:
        node = matches[0]
        which = _failed_precondition(node, loc.require)
        if which is not None:
            return PreconditionFailed(which=which)
        return Unique(node=node)

    fallback_failures: list[str] = []
    for fallback in loc.fallbacks:
        result = resolve_against(fallback, nodes)
        if result.kind != "not_found":
            return result
        fallback_failures.append(result.reason)

    if matches:
        return Ambiguous(count=len(matches))

    reason = (
        f"no node matched strategy={loc.strategy!r} role={loc.role!r} "
        f"name={loc.name!r} on surface_path={loc.surface_path!r}"
    )
    if fallback_failures:
        reason += (
            f"; {len(fallback_failures)} fallback(s) also tried and failed to match: "
            + "; ".join(fallback_failures)
        )
    return NotFound(reason=reason)
