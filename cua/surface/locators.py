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
    ancestor_positions,
)


def ancestors_of(node: Node, nodes: list[Node]) -> list[Node]:
    """Returns `node`'s enclosing nodes within `nodes`, nearest ancestor first.

    The one containment rule `synthesize` and `resolve_against` both use: `nodes[i]`
    encloses `node` when `i` precedes `node`'s position in the list and `nodes[i]` is a
    proper ancestor by depth. The actual depth-walk (R22) is `cua.surface.models.
    ancestor_positions`, shared with `cua.surface.snapshot._scrub_ancestor_names` -- this
    function only maps `node` to its position and the returned positions back to `Node`
    objects.

    `nodes` should already be filtered to one `surface_path` (as `synthesize` and
    `resolve_against` both do before calling this) -- containment across frames is
    meaningless, since two frames' node lists are simply concatenated, not nested.

    Invariant this relies on and does not check: `nodes` must be in true document
    (pre-order) order -- the order `parse_aria_snapshot` emits, where every node's
    ancestors precede it and depth only rises and falls with actual nesting. A caller that
    passes a reordered or arbitrarily filtered list (sorted by name, shuffled, every third
    node dropped) gets silently wrong ancestors back, not an error: the depth/position walk
    has no way to detect that its input no longer reflects the real tree. This is why
    `WebSurface.act_on_index` (I2) synthesizes against the raw, unfiltered node population
    rather than the filtered-and-renumbered one the model was shown -- the model-facing list
    is exactly the kind of arbitrarily-filtered input this paragraph warns about.
    """
    position = _position_of(node, nodes)
    depths = [n.depth for n in nodes]
    return [nodes[i] for i in ancestor_positions(position, depths)]


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


def matches(node: Node, *, strategy: Strategy, role: str | None, name: str | None,
            name_match: NameMatch) -> bool:
    """Whether `node` satisfies these match criteria (role/name), ignoring scope.

    `role_name` criteria always carry a role (the model enforces it on a `Locator`), so the
    role check always applies for them. `text` criteria deliberately carry `role=None` (D10:
    the target application carries no ARIA roles for the outcome/recovery text they match),
    so the role check is skipped rather than failing on a role that was never set. `ax_path`
    criteria may or may not carry a role, so the same "skip if None" rule covers both.

    `text`-strategy criteria match against `_text_of` (name, falling back to value) rather
    than `node.name` alone: a `text` locator synthesized from an unnamed node's `value`
    (phase 1's pinned unnamed-input hostile case) must be able to match that same node
    again, and `node.name` would never equal that value.
    """
    if role is not None and node.role != role:
        return False
    candidate = _text_of(node) if strategy == "text" else node.name
    return _name_matches(candidate, name, name_match)


def _matches_locator(node: Node, loc: Locator) -> bool:
    """Whether `node` satisfies `loc`'s own match criteria (role/name), ignoring scope.

    Delegates to `matches`, extracted from this function's former body so that
    `cua.replay.settle` (which must act only through the `Surface` protocol, never a
    `Locator`, to check a bare `Matcher`) can share the exact same predicate.
    """
    return matches(node, strategy=loc.strategy, role=loc.role, name=loc.name,
                   name_match=loc.name_match)


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
    # R24: visibility is a live-only property, and this is the pure-layer half of that
    # rule. `Node` carries no visibility flag, and this function genuinely cannot check one
    # -- a node's mere presence in an Observation is the only signal available offline, so
    # `PreconditionFailed(which="visible")` can never originate here. That is not a gap:
    # the live half of the same rule -- re-checking visibility against the actual page,
    # because a snapshot can go stale between observation and action -- lives in
    # `cua.surface.web._failed_live_precondition`, which is where `require.visible` is
    # actually enforced.
    return None


def _resolve_direct(loc: Locator, nodes: list[Node]) -> Resolution:
    """Resolves `loc` against `nodes` using only its own match criteria, `scope`, `ordinal`
    and `require` -- never touching `loc.fallbacks`. This is one "attempt" in the ordered
    sequence `resolve_against` works through; the fallback chain is that function's concern,
    not this one's, so that trying a fallback can never accidentally re-trigger its own
    fallback list twice.
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

    if matches:
        return Ambiguous(count=len(matches))

    return NotFound(
        reason=(
            f"no node matched strategy={loc.strategy!r} role={loc.role!r} "
            f"name={loc.name!r} on surface_path={loc.surface_path!r}"
        )
    )


def resolve_against(loc: Locator, nodes: list[Node]) -> Resolution:
    """Resolves `loc` against `nodes`, strictly (spec §3.4 rule 3).

    Builds one ordered list of attempts -- `loc` itself, then each of `loc.fallbacks` in
    turn (each fallback resolved through this same function, so a fallback's own fallbacks
    are exhausted too) -- and answers in two separate passes over that list, deliberately
    not conflated into one loop:

    1. **Look for a `unique`.** The fallback list is the author's stated order of
       preference, and spec §3.4 rule 3's "then fallbacks" means working through all of
       them, not stopping at the first one that produces any definite-ish answer. The first
       attempt (primary first, then fallbacks in order) that resolved `unique` -- with
       `require` already checked by `_resolve_direct` -- wins and returns immediately.
    2. **Only if nothing resolved uniquely, decide what to report.** Returns the first
       non-`not_found` result in that same attempt order. A found-but-disabled control
       (`precondition_failed`) or a genuinely ambiguous match reached through any attempt is
       returned as-is, never collapsed into a generic `not_found` -- that silent-failure
       class is exactly what `require` and `ambiguous` exist to surface. Ranking by
       attempt order rather than by some severity ordering between `ambiguous` and
       `precondition_failed` keeps this predictable: whichever attempt the author listed
       first is the one whose story gets told.

    If every attempt comes back `not_found`, the result is `not_found` too, with a reason
    naming the primary and every fallback that was tried and how each failed to match. This
    function never guesses between multiple candidates at any point in that process.

    E8: cycle-guarded. `a.fallbacks = [b]; b.fallbacks = [a]` is constructible with plain
    Pydantic attribute assignment -- `Locator.fallbacks` is an ordinary assignable field, so
    no `model_construct` or `model_copy` bypass is needed -- and the recursion below reached
    `RecursionError` on it. Phase 2 triaged this as a deferred minor partly on the belief
    that building one required a bypass, which was wrong. Phase 3's compiler is the first
    real producer of `fallbacks` and phase 4 replays fallback chains, so it is fixed here.
    """
    return _resolve_with_fallbacks(loc, nodes, frozenset())


def _resolve_with_fallbacks(
    loc: Locator, nodes: list[Node], chain: frozenset[int]
) -> Resolution:
    """`resolve_against`'s body, carrying the set of locator identities on the current path.

    The guard is keyed on object identity and scoped to the **current path**, not to the
    whole walk. Two points, both deliberate:

    - Identity, not equality: two distinct `Locator` objects can compare equal under
      Pydantic and both deserve their own attempt; only revisiting the same object is a loop.
    - Per-path, not global: a locator legitimately reachable through two different fallback
      branches (a diamond) is still tried on each, exactly as before. Only a true back edge
      -- a locator that is its own ancestor in the chain -- is pruned, so this changes the
      answer for no input that previously terminated.

    A pruned edge is silent here. It is an authoring defect rather than a resolution
    outcome, and inventing a `Resolution` kind for it would widen a closed vocabulary that
    spec §3.4 keeps deliberately small; the artifact validator is the right place to catch
    one before replay ever sees it.
    """
    chain = chain | {id(loc)}
    attempts: list[Resolution] = [_resolve_direct(loc, nodes)]
    for fallback in loc.fallbacks:
        if id(fallback) in chain:
            continue
        attempts.append(_resolve_with_fallbacks(fallback, nodes, chain))

    for result in attempts:
        if result.kind == "unique":
            return result

    for result in attempts:
        if result.kind != "not_found":
            return result

    not_found_reasons: list[str] = []
    for result in attempts:
        if result.kind == "not_found":
            not_found_reasons.append(result.reason)

    reason = not_found_reasons[0]
    if len(not_found_reasons) > 1:
        reason += (
            f"; {len(not_found_reasons) - 1} fallback(s) also tried and failed to match: "
            + "; ".join(not_found_reasons[1:])
        )
    return NotFound(reason=reason)
