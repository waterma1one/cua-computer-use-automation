"""The web surface: the only file in this system allowed to import Playwright.

Implements the `Surface` protocol (`cua/surface/base.py`) against a live Chromium page
driven through Playwright's synchronous API. This module fetches accessibility snapshots
and performs actions, but it is never the place that decides which node a locator means:
`resolve()` takes a fresh snapshot of the target frame, parses it with
`cua.surface.snapshot.parse_aria_snapshot`, and hands the matching decision to
`cua.surface.locators.resolve_against` -- the one matching semantics in the system, also
used by synthesis. Reimplementing scope, ordinal, `name_match`, and the ancestor-
containment rule in Playwright terms would be a second matching engine that could disagree
with the first about which control "the Savings row" means, and that disagreement is a
system that silently clicks the wrong thing.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Browser, Dialog, Frame, Page, Playwright, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator as PlaywrightLocator

from cua.surface.base import NavigationGuard, StaleObservationError, Surface, SurfaceError
from cua.surface.locators import resolve_against, synthesize
from cua.surface.models import (
    Action,
    ActionKind,
    ActionResult,
    EvidenceFrame,
    Locator,
    Node,
    NotFound,
    Observation,
    PreconditionFailed,
    Require,
    Resolution,
    SurfaceSegment,
)
from cua.surface.snapshot import parse_aria_snapshot, scrub_protected_values

# Also fix: `StaleObservationError` lives in `base.py` precisely so a caller that must not
# import Playwright (phase 3's compiler, phase 4's replay engine) can still catch it.
# Re-exporting it from this, the one Playwright-importing module, would invite exactly the
# import it exists to avoid -- so it is imported here (raised by `act_on_index`) but not
# re-exported.
__all__ = [
    "ObservationBudget",
    "SessionBrowser",
    "WebSurface",
    "close_session_page",
    "launch_page",
    "open_session_page",
]

# Also fix: a real Observation's generation is always >= 1 (spec §3.1: `observe()`
# increments before returning). This sentinel is what `capture()` reports before the first
# `observe()` call -- unambiguously distinct from any real generation regardless of whether
# a future numbering scheme ever started at 0.
_NEVER_OBSERVED = -1

# A blocked navigation (most realistically: the target app's own `dialog` fault, whose
# inline `window.confirm(...)` stalls the load event) must fail fast enough for `act()`
# to turn it into an honest ActionResult rather than eating the default 30s timeout.
_NAVIGATION_TIMEOUT_MS = 5_000

# act_on_index's `action: str` parameter (fixed by the brief; not ours to widen) has
# nowhere to carry a value. Silently defaulting one of these to "" or None would perform
# the wrong action -- e.g. blanking a field -- while still reporting `ok=True`, which is
# the single worst outcome this system can produce. Refuse instead of guessing.
_VALUE_REQUIRED_KINDS: frozenset[str] = frozenset({"fill", "select", "press_key"})

# Roles that are always kept in an Observation regardless of whether they carry an
# accessible name. A control the model may need to act on -- the unnamed textbox phase 1
# pinned as required hostile markup, for instance -- must still be observable even when it
# has no label at all, so "interactive" is checked before "labelled or text-bearing".
_INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "textbox",
        "searchbox",
        "combobox",
        "listbox",
        "checkbox",
        "radio",
        "switch",
        "slider",
        "spinbutton",
        "link",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "tab",
        "option",
    }
)


@dataclass
class ObservationBudget:
    """Caps how many nodes a single `observe()` call returns."""

    max_nodes: int = 120


def _budget_frame_fair(
    frame_relevant: list[list[Node]], max_nodes: int
) -> tuple[list[Node], bool]:
    """Distributes `max_nodes` across `frame_relevant` (one relevant-node list per frame, in
    `page.frames` order) round-robin, rather than slicing the concatenation positionally.

    Deferred-minor 6, promoted: `page.frames` enumerates `nav` before `content`, so a plain
    `concatenated[:max_nodes]` under a tight budget returned only `nav`'s branding chrome
    and dropped the entire `content` frame -- verified live. Taking nodes one at a time from
    each frame in turn (preserving each frame's own document order) means every frame with
    any relevant nodes at all contributes something before any single frame can exhaust the
    whole budget, so a tight cap degrades every frame instead of deleting one outright.
    """
    total = sum(len(frame_nodes) for frame_nodes in frame_relevant)
    if total <= max_nodes:
        return [n for frame_nodes in frame_relevant for n in frame_nodes], False

    selected: list[list[Node]] = [[] for _ in frame_relevant]
    cursors = [0] * len(frame_relevant)
    count = 0
    while count < max_nodes:
        progressed = False
        for i, frame_nodes in enumerate(frame_relevant):
            if count >= max_nodes:
                break
            if cursors[i] < len(frame_nodes):
                selected[i].append(frame_nodes[cursors[i]])
                cursors[i] += 1
                count += 1
                progressed = True
        if not progressed:
            break
    return [n for frame_nodes in selected for n in frame_nodes], True


def _snapshot_frame(frame: Frame) -> str:
    """Returns one frame's raw accessibility-tree snapshot YAML.

    Snapshots `:root` (the `<html>` element), not `body`. The classic `<frameset>` shell
    document this app's `/` renders has zero `<body>` elements at all -- a frameset
    replaces `body`, it does not merely hide it -- confirmed empirically: `frame.locator
    ("body")` on that document blocks until Playwright's 30s default timeout because the
    element it is waiting for can never attach. `:root` resolves on every frame, frameset
    shell included, and the accessibility subtree it returns for an ordinary `<body>`
    document is identical content-wise to `body`'s, just wrapped one level deeper under a
    single unnamed `document` node -- which `_is_relevant` drops from the Observation
    the same way it drops any other nameless, non-interactive container.
    """
    return frame.locator(":root").aria_snapshot()


def _surface_path_for(frame: Frame) -> list[SurfaceSegment]:
    """Maps a Playwright frame to its `SurfaceSegment` path.

    `page.frames` includes the main (top-level frameset) document, whose `.name` is the
    empty string; that maps to `[window:main]`. A named child frame -- `nav`, `content` --
    maps to `[window:main, frame:<name>]`.
    """
    if frame.name == "":
        return [SurfaceSegment(kind="window", name="main")]
    return [
        SurfaceSegment(kind="window", name="main"),
        SurfaceSegment(kind="frame", name=frame.name),
    ]


def _is_relevant(node: Node) -> bool:
    """Interactive, labelled, or text-bearing -- everything else is structural noise
    (a bare `table`/`rowgroup` wrapper with no name of its own)."""
    return node.role in _INTERACTIVE_ROLES or node.name is not None or node.value is not None


def _text_of(node: Node) -> str | None:
    """The text a `text`-role node is identified by when mapping it to a live handle:
    mirrors `cua.surface.locators.text_of` (name, falling back to value), because a
    `text`-strategy locator's resolved node is matched against exactly that text.
    """
    return node.name if node.name else node.value


def _handle_group_for(frame: Frame, node: Node) -> PlaywrightLocator | None:
    """Returns the Playwright locator *group* (before `.nth(...)`) that addresses nodes of
    `node`'s shape, or `None` if this surface has no way to address that shape at all.

    CRITICAL 2: a `role="text"` node (a bare accessibility-tree text node, Playwright's own
    synthetic category for it) is not addressable through `get_by_role` at all --
    `get_by_role("text")` matches nothing, silently, because "text" is not a queryable ARIA
    role. `get_by_text` is the Playwright counterpart for this shape.

    For every other role, `get_by_role(role, name=...)` is used, with `name=""` (not
    "no filter") when `node.name` is `None` -- CRITICAL 1's fix. Playwright's own accessible
    name computation treats "no name" as an empty-string exact match, the same way this
    surface's own parser normalizes an empty accessible name to `None`
    (`cua.surface.snapshot.parse_aria_snapshot`), so `get_by_role(role, name="", exact=True)`
    addresses exactly the population of *unnamed* nodes of that role -- never named ones too.
    """
    if node.role == "text":
        text = _text_of(node)
        if text is None:
            return None
        return frame.get_by_text(text)
    role = cast(Any, node.role)
    return frame.get_by_role(role, name=node.name if node.name is not None else "", exact=True)


def _population_for(node: Node, nodes: list[Node]) -> list[Node]:
    """The `nodes` sharing `node`'s identity, in the same terms `_handle_group_for` uses to
    build the live locator group -- the population `.nth(position)` indexes into must be
    computed the same way the group is addressed, or a position computed in one population
    lands on a different element in the other (CRITICAL 1's actual failure mode).
    """
    if node.role == "text":
        text = _text_of(node)
        return [n for n in nodes if n.role == "text" and _text_of(n) == text]
    return [n for n in nodes if n.role == node.role and n.name == node.name]


def _handle_for(frame: Frame, node: Node, nodes: list[Node]) -> PlaywrightLocator | None:
    """Maps a resolved `Node` to a live Playwright handle for acting on it, or `None` if
    this surface cannot address it at all: `node` is not actually present in `nodes`
    (defensive -- every real caller passes a `nodes` list `node` was drawn from, so this
    should be unreachable), `node`'s shape has no live counterpart (`_handle_group_for`
    returns `None`), or the resulting handle group does not actually contain `position`
    live elements. That last check matters on its own: CRITICAL 2 showed an empty handle
    group answering `.is_visible() == False` for `.nth(k)` on a group with zero elements,
    which `_failed_live_precondition` would then misreport as `precondition_failed(visible)`
    -- a present, unique, visible node reported as failing a check it never actually ran.
    Checking `.count()` here means an unaddressable node always becomes `NotFound`, never a
    false precondition failure.

    `.nth(position)` where `position` is `node`'s position within `_population_for(node,
    nodes)` -- the same identity `resolve_against` used to pick `node` out in the first
    place (role/name, or text for a `text`-role node), just re-expressed as a live locator
    rather than a data match, and computed against the *same* population `_handle_group_for`
    addresses.
    """
    population = _population_for(node, nodes)
    position = next((i for i, n in enumerate(population) if n.index == node.index), None)
    if position is None:
        return None

    group = _handle_group_for(frame, node)
    if group is None:
        return None

    if group.count() <= position:
        return None

    return group.nth(position)


def _failed_live_precondition(
    handle: PlaywrightLocator, require: Require
) -> Literal["visible", "enabled"] | None:
    """Checks `require` against the live handle rather than the snapshot (R24's live half).

    The pure layer (`cua.surface.locators._failed_precondition`) cannot check visibility at
    all -- a node's mere presence in a snapshot is the only offline signal available, and
    that snapshot has already excluded elements the browser considers hidden. This is the
    one place `require.visible` is actually enforced, re-verified here (rather than trusted
    from the snapshot) because a snapshot can go stale between observation and action.
    `enabled` is deliberately checked at both layers: the pure layer's check is what a
    locator's `require.enabled=True` can fail during synthesis-time reasoning that never
    touches a browser, and this check re-verifies it live for the same staleness reason.
    """
    if require.visible and not handle.is_visible():
        return "visible"
    if require.enabled and not handle.is_enabled():
        return "enabled"
    return None


def _display_url(url: str) -> str:
    """`url` with any userinfo stripped from its netloc, for a violation reason a human
    reads (evidence, `SurfaceError` messages) -- a credential embedded in the URL itself
    (`http://user:pw@host/...`) must never appear there. Standard library only: this is
    the surface's own rendering concern, not `cua.policy`'s (the guard it calls still
    receives the original `url` and strips userinfo itself when it builds an origin).

    Cuts the netloc's own `...@` prefix (`rpartition` on `@`, keeping the tail) rather
    than re-parsing it via `hostname`/`port` -- re-parsing drops an IPv6 host's brackets
    and can raise `ValueError` on a malformed port before `route.abort` ever runs, which
    would surface as an unhandled exception out of a Playwright event handler instead of
    a denial.
    """
    parts = urlsplit(url)
    netloc = parts.netloc.rpartition("@")[2]
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


class WebSurface:
    """A `Surface` (see `cua/surface/base.py`) backed by one live Playwright `Page`.

    `navigation_guard`, when given, enforces the deployment allowlist live (Phase 5 /
    E2-E4): every application-initiated navigation, on every frame, is checked, and the
    first denial freezes the surface (`allowlist_violation()` becomes non-`None`; every
    later `act`/`act_on_index` raises `SurfaceError`). `None` (the default) leaves the
    surface unenforced, which is what phase-2/4 fixtures still construct.

    Constructing two guarded `WebSurface`s on pages that share one browser context is not
    supported: `page.context.route(...)` is registered per context, so the second
    surface's handler replaces the first's rather than adding to it, and only the second
    surface's guard is actually enforced. Each guarded surface needs its own context.
    """

    def __init__(
        self, page: Page, budget: ObservationBudget | None = None, *,
        navigation_guard: NavigationGuard | None = None,
    ) -> None:
        self.page = page
        self.budget = budget or ObservationBudget()
        self._generation = _NEVER_OBSERVED
        # I2: two parallel node lists from the same `observe()` call, deliberately kept
        # distinct. `_last_nodes` is the filtered-and-renumbered list the model was shown
        # (what `Observation.nodes` contains) -- the "arbitrarily filtered list" `ancestors_
        # of`'s docstring warns produces silently wrong containment. `_last_raw_nodes` is
        # the complete, unfiltered, un-renumbered parse, in true document order, and is what
        # `act_on_index` uses for `synthesize`'s scope/ancestor reasoning. `_last_raw_for_
        # observed[i]` is the raw node underlying `_last_nodes[i]` (same object, original
        # index), so `act_on_index` can map a model-facing index back to its raw identity
        # without ever mutating a raw node's own index.
        self._last_nodes: list[Node] = []
        self._last_raw_nodes: list[Node] = []
        self._last_raw_for_observed: list[Node] = []
        self._pending_dialog: Dialog | None = None
        # Registering a listener stops Playwright's default behaviour of auto-dismissing
        # native dialogs, so one raised by the target app (e.g. the `dialog` fault) stays
        # open until `act()` is asked to dismiss it, rather than vanishing unnoticed.
        self.page.on("dialog", self._on_dialog)
        # Phase 5 / E2-E4: live allowlist enforcement, two points. The context-level route
        # sees every navigation request the page itself initiates -- link, form, `goto`,
        # and a popup's first request (which a page-level route would miss) -- and aborts a
        # denied one before it leaves, so the mutating GET never reaches the server.
        # `is_navigation_request()` in `_on_route` means a subresource fetched by the page
        # (a script, an image, an XHR/fetch) is never gated here -- only a request that
        # would change what URL the frame is on. `framenavigated` sees what interception
        # cannot: a server redirect whose hop is followed inside the browser and lands on a
        # denied URL. That request has already fired by the time it is visible; it is
        # detected and the surface frozen, and the write-up states that as the cost. Every
        # frame on every page the context opens is checked (`_on_new_page` extends
        # `framenavigated` to a popup, which is a separate `Page` on the same context) --
        # not only the main frame of the one page this surface was constructed with.
        # `None` means unenforced (what phase-2/4 fixtures construct); the CLI always
        # supplies a guard.
        self._guard = navigation_guard
        self._violation: str | None = None
        if navigation_guard is not None:
            self.page.context.route("**/*", self._on_route)
            self.page.on("framenavigated", self._on_frame_navigated)
            self.page.context.on("page", self._on_new_page)

    def _on_dialog(self, dialog: Dialog) -> None:
        self._pending_dialog = dialog

    def _on_new_page(self, page: Page) -> None:
        # A popup (`target=_blank`, `window.open`) is a page of the same context. Its first
        # request is already intercepted at the context route, but a server redirect it
        # follows is visible only to its own `framenavigated`, so the detection point is
        # attached to every page the context opens, not only the one this surface drives.
        page.on("framenavigated", self._on_frame_navigated)

    def _deny_reason(self, url: str) -> str | None:
        """The guard's verdict for `url`, or `None`. Only `http`/`https` URLs are checked
        -- `about:blank` and the `chrome-error://` page an aborted navigation lands on are
        not navigations to an origin. A guard that raises is a denial naming the error:
        an enforcement point that fails open is not one.
        """
        if self._guard is None or not url.startswith(("http://", "https://")):
            return None
        try:
            return self._guard(url)
        except Exception as exc:  # fail closed on any guard failure, whatever it is
            return f"the navigation guard failed on {_display_url(url)!r}: {exc}"

    def _freeze(self, reason: str) -> None:
        if self._violation is None:
            self._violation = reason

    def _on_route(self, route: Route) -> None:
        request = route.request
        if not request.is_navigation_request():
            route.continue_()
            return
        reason = self._deny_reason(request.url)
        if reason is None:
            route.continue_()
            return
        self._freeze(f"navigation to {_display_url(request.url)!r} refused before the "
                     f"request was sent: {reason}")
        route.abort("blockedbyclient")

    def _on_frame_navigated(self, frame: Frame) -> None:
        reason = self._deny_reason(frame.url)
        if reason is not None:
            self._freeze(f"the application navigated to {_display_url(frame.url)!r}, "
                         f"which is refused: {reason}")

    def allowlist_violation(self) -> str | None:
        return self._violation

    def _refuse_if_frozen(self) -> None:
        if self._violation is not None:
            raise SurfaceError(f"the session is frozen after an allowlist violation: "
                               f"{self._violation}")

    # ---- perception --------------------------------------------------------

    def observe(self) -> Observation:
        try:
            frame_relevant: list[list[Node]] = []
            raw_nodes: list[Node] = []
            index = 0
            for frame in self.page.frames:
                path = _surface_path_for(frame)
                raw = _snapshot_frame(frame)
                frame_nodes = parse_aria_snapshot(raw, path, start_index=index)
                raw_nodes.extend(frame_nodes)
                index += len(frame_nodes)
                frame_relevant.append([n for n in frame_nodes if _is_relevant(n)])
        except PlaywrightError as exc:
            raise SurfaceError(f"observe() failed while snapshotting: {exc}") from exc

        selected, truncated = _budget_frame_fair(frame_relevant, self.budget.max_nodes)
        # Task 2's parser contract is dense, zero-based indices; filtering (and, now,
        # frame-fair truncation) necessarily leaves gaps, so the model-facing list is
        # renumbered to close them. `model_copy` rather than in-place mutation (I2): the
        # renumbered objects are copies, so `raw_nodes`' own indices -- and the objects in
        # `selected`, which are the *same* objects as in `raw_nodes` -- are never touched.
        renumbered = [n.model_copy(update={"index": i}) for i, n in enumerate(selected)]

        self._generation = 1 if self._generation == _NEVER_OBSERVED else self._generation + 1
        self._last_nodes = renumbered
        self._last_raw_nodes = raw_nodes
        self._last_raw_for_observed = selected
        return Observation(generation=self._generation, nodes=renumbered, truncated=truncated)

    def capture(self) -> EvidenceFrame:
        # Both halves of a capture raise on failure rather than degrading quietly. A
        # screenshot that could not be taken is not the same thing as a screenshot that
        # was never meant to exist, and this is evidence written to disk in a later phase
        # -- a silently-missing image reads to whoever reviews /evidence/ as "nothing to
        # see here" rather than "capture failed here", which is a worse failure than a
        # loud one. Consistent with the raw-snapshot half just below, which always raised.
        try:
            image_png: bytes | None = self.page.screenshot()
        except PlaywrightError as exc:
            raise SurfaceError(f"capture() failed while screenshotting: {exc}") from exc
        try:
            raw = "\n".join(_snapshot_frame(frame) for frame in self.page.frames)
        except PlaywrightError as exc:
            raise SurfaceError(f"capture() failed while snapshotting: {exc}") from exc
        return EvidenceFrame(
            generation=self._generation,
            image_png=image_png,
            snapshot_yaml=scrub_protected_values(raw),
        )

    def pending_dialog(self) -> str | None:
        # E6/E20: the bare `dialog.message`, not `_dialog_reason()`'s longer sentence -- a
        # caller (the replay engine's settle loop) matches this against a `Matcher`'s
        # `name`/`name_match`, and `_dialog_reason()`'s wrapping text would never match a
        # `Recovery.detect` authored against the dialog's own wording.
        dialog = self._pending_dialog
        return dialog.message if dialog is not None else None

    # ---- resolution ---------------------------------------------------------

    def _frame_for(self, surface_path: list[SurfaceSegment]) -> Frame | None:
        """Resolves `surface_path` to a live Playwright frame, or `None` if it names a frame
        that is not currently attached.

        I5: a frame disappearing after a navigation is routine in a frameset app, so that
        case is not an exception at all -- `_resolve_with_handle` turns a `None` here into a
        `NotFound` resolution, the same outcome vocabulary already used for "the target
        locator's node is not there." Raising `SurfaceError` is reserved for a surface_path
        *shape* this surface does not know how to interpret -- a genuine implementation-
        level anomaly a frameset navigation cannot produce, unlike a vanished frame.
        """
        if len(surface_path) == 1 and surface_path[0].kind == "window":
            return self.page.main_frame
        if (
            len(surface_path) == 2
            and surface_path[0].kind == "window"
            and surface_path[1].kind == "frame"
        ):
            return self.page.frame(name=surface_path[1].name)
        raise SurfaceError(f"unsupported surface_path shape: {surface_path!r}")

    def _resolve_with_handle(
        self, locator: Locator
    ) -> tuple[Resolution, PlaywrightLocator | None]:
        """Resolves `locator` and, on a unique result, maps it to a live handle in the
        same pass -- one snapshot, not two.

        `resolve()` and `act()` both need this, and an earlier version of `act()` took a
        *second*, separate fresh snapshot of the same frame just to build the handle after
        `resolve()` had already taken one. On a dynamic page the two snapshots are not
        guaranteed to agree, which meant the second snapshot's node list could fail to
        contain the first's resolved node at all. Snapshotting once and reusing the same
        `nodes` list for both the resolution and the handle removes that mismatch by
        construction, rather than merely guarding against it.
        """
        try:
            frame = self._frame_for(locator.surface_path)
        except PlaywrightError as exc:
            raise SurfaceError(f"resolve() failed while locating the frame: {exc}") from exc

        if frame is None:
            return (
                NotFound(
                    reason=(
                        f"no live frame for surface_path {locator.surface_path!r}; it may "
                        "have disappeared after a navigation"
                    )
                ),
                None,
            )

        try:
            raw = _snapshot_frame(frame)
            nodes = parse_aria_snapshot(raw, locator.surface_path, start_index=0)
        except PlaywrightError as exc:
            raise SurfaceError(f"resolve() failed while snapshotting: {exc}") from exc

        result = resolve_against(locator, nodes)
        if result.kind != "unique":
            return result, None

        try:
            handle = _handle_for(frame, result.node, nodes)
        except PlaywrightError as exc:
            raise SurfaceError(f"resolve() failed while mapping to a live handle: {exc}") from exc
        if handle is None:
            # CRITICAL 2's general case: `result.node` was drawn from `nodes` by
            # `resolve_against` itself, but this surface may still have no live counterpart
            # for its shape (an unaddressable strategy) or find its handle group does not
            # actually contain it (`_handle_for`'s own `.count()` check). Either way, the
            # honest answer is `NotFound`, never a `PreconditionFailed` inferred from an
            # empty handle.
            return (
                NotFound(reason="resolved node could not be mapped to a live handle"),
                None,
            )

        try:
            which = _failed_live_precondition(handle, locator.require)
        except PlaywrightError as exc:
            raise SurfaceError(f"resolve() failed while checking preconditions: {exc}") from exc
        if which is not None:
            return PreconditionFailed(which=which), None
        return result, handle

    def resolve(self, locator: Locator) -> Resolution:
        result, _handle = self._resolve_with_handle(locator)
        return result

    # ---- action ---------------------------------------------------------------

    def _dialog_reason(self) -> str:
        dialog = self._pending_dialog
        if dialog is None:
            return "a dialog is pending; dismiss it before navigating"
        return (
            f"a dialog is pending ({dialog.type}: {dialog.message!r}); "
            "dismiss it before navigating"
        )

    def act(self, action: Action) -> ActionResult:
        self._refuse_if_frozen()
        if action.kind == "navigate":
            if action.value is None:
                return ActionResult(ok=False, action=action, read_value=None)
            if self._pending_dialog is not None:
                return ActionResult(ok=False, action=action, read_value=self._dialog_reason())
            try:
                self.page.goto(action.value, timeout=_NAVIGATION_TIMEOUT_MS)
                self.page.wait_for_load_state("networkidle", timeout=_NAVIGATION_TIMEOUT_MS)
            except PlaywrightError as exc:
                # The one realistic way a navigation stalls on this app: an inline
                # `window.confirm(...)` (the `dialog` fault) blocks the load event
                # entirely. Detected, not merely tolerated -- a short timeout turns that
                # into a fast, honest ActionResult naming the pending dialog rather than
                # a 30s hang followed by a raw Playwright exception escaping `act()`.
                if self._pending_dialog is not None:
                    return ActionResult(ok=False, action=action, read_value=self._dialog_reason())
                raise SurfaceError(f"navigate to {action.value!r} failed: {exc}") from exc
            return ActionResult(ok=True, action=action, read_value=None)

        if action.kind == "dismiss_dialog":
            dialog = self._pending_dialog
            if dialog is None:
                return ActionResult(ok=False, action=action, read_value=None)
            try:
                dialog.dismiss()
            except PlaywrightError as exc:
                raise SurfaceError(f"dismiss_dialog failed: {exc}") from exc
            self._pending_dialog = None
            return ActionResult(ok=True, action=action, read_value=None)

        if action.locator is None:
            return ActionResult(ok=False, action=action, read_value=None)

        result, handle = self._resolve_with_handle(action.locator)
        if result.kind != "unique" or handle is None:
            return ActionResult(ok=False, action=action, read_value=None)

        try:
            if action.kind == "click":
                handle.click()
            elif action.kind == "fill":
                handle.fill(action.value or "")
            elif action.kind == "select":
                handle.select_option(action.value)
            elif action.kind == "press_key":
                handle.press(action.value or "")
            elif action.kind == "wait_for":
                handle.wait_for(state="visible")
            elif action.kind == "read":
                # I4: the value, and only the value -- an empty field must read back empty,
                # not its own accessible name. `result.node.value` is already `None` for a
                # protected node (spec §3.7.1), so this never leaks a password either.
                return ActionResult(ok=True, action=action, read_value=result.node.value)
            else:
                return ActionResult(ok=False, action=action, read_value=None)
        except PlaywrightError as exc:
            raise SurfaceError(f"{action.kind} failed: {exc}") from exc

        return ActionResult(ok=True, action=action, read_value=None)

    # ---- discovery-time entry point --------------------------------------------

    def act_on_index(self, generation: int, index: int, action: str) -> ActionResult:
        """The discovery-time counterpart to `act()`: the model names an index from an
        `Observation` it was just handed, rather than building a `Locator` itself.

        `generation` must match the observation this surface most recently produced --
        any mismatch (an older generation the page has changed under, or a fabricated
        future one) raises `StaleObservationError` rather than executing (spec §3.1).

        I2: the fresh locator is synthesized (`cua.surface.locators.synthesize`, the same
        synthesis phase 3's compiler uses) against `self._last_raw_nodes` -- the complete,
        unfiltered population from this same observation -- not `self._last_nodes`, the
        filtered-and-renumbered list the model was shown. `ancestors_of`'s own docstring
        warns that an arbitrarily filtered list yields silently wrong containment, not an
        error; synthesizing against the model-facing list risks exactly that; the raw list
        is the one `resolve_against` (via `_resolve_with_handle`'s own fresh, unfiltered
        parse) actually resolves against, so synthesis and resolution must reason about the
        same population `resolve_against` will use. `index` names a position in the
        model-facing `Observation.nodes`; `self._last_raw_for_observed[index]` is the raw
        node underlying it, drawn from the same objects `self._last_raw_nodes` holds.

        Executed through `act()`, so there is exactly one implementation of "perform an
        action" underneath both entry points.

        This entry point's `action` parameter is a bare kind string with no way to carry
        a `value` -- the brief pins this signature, and widening it is phase 3's call to
        make, not this task's. `fill`, `select`, and `press_key` all need a value to do
        anything meaningful; asked for one of them here, this refuses rather than
        defaulting to an empty or absent value, which would silently perform the wrong
        action (e.g. blank a field) while still reporting success.
        """
        self._refuse_if_frozen()
        if generation != self._generation:
            raise StaleObservationError(
                f"observation generation {generation} does not match the current "
                f"generation {self._generation}"
            )
        if action in _VALUE_REQUIRED_KINDS:
            placeholder = Action(kind=cast(ActionKind, action), locator=None, value=None)
            return ActionResult(
                ok=False,
                action=placeholder,
                read_value=(
                    f"act_on_index cannot supply a value for {action!r}; use the "
                    "locator-driven act() instead"
                ),
            )
        if not (0 <= index < len(self._last_raw_for_observed)):
            raise ValueError(f"no node with index {index} in the current observation")
        raw_node = self._last_raw_for_observed[index]
        locator = synthesize(raw_node, self._last_raw_nodes)
        built = Action(kind=cast(ActionKind, action), locator=locator, value=None)
        return self.act(built)


def _typed_as_a_surface(page: Page) -> Surface:
    """I7: exists only for `mypy cua mockapp` -- never called at runtime.

    `Surface` (`cua/surface/base.py`) is a `Protocol`, so structural drift between it and
    `WebSurface` (a method renamed, dropped, or given an incompatible signature) is
    otherwise invisible to a type checker unless something, somewhere, actually assigns a
    `WebSurface` to a `Surface`-typed name. R25 folded `act_on_index` into the protocol
    specifically so a caller could type against `Surface` alone without importing this,
    the only Playwright-importing module -- which is exactly why this check has to live
    here rather than in `cua/surface/base.py` itself: it is the one place in the system
    both types are legitimately in scope together. The runtime half of this same
    conformance check (`isinstance(surface, Surface)`) lives in
    `tests/surface/test_web_surface.py`.
    """
    return WebSurface(page)


@contextmanager
def launch_page(base_url: str) -> Iterator[Page]:
    """Launches Chromium, opens one page against `base_url`, and tears both down on exit.

    E13: `base_url` is passed to `browser.new_page(base_url=...)` -- Playwright's own
    per-page base for relative navigation -- rather than baked into a first `page.goto`
    call, so a caller (`cua.cli`'s `replay` command) can drive an artifact whose recorded
    `Target.path` is host-relative (E2) against whichever instance's base URL it was given.

    This factory is the one place `cli.py` reaches into this module by name (`from
    cua.surface.web import launch_page`) rather than through a qualified `web.launch_page
    (...)` call, specifically so a test can `monkeypatch.setattr(cli_module, "launch_page",
    ...)` and have the replacement actually take effect (E22) -- `cli.py` itself still
    never names the driver package directly, because this is the only module that does.
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(base_url=base_url)
            try:
                yield page
            finally:
                page.close()
        finally:
            browser.close()


@dataclass
class SessionBrowser:
    """The three handles `open_session_page` opened, held together so `close_session_page`
    can tear all three down -- Playwright's sync API gives no public way back from a `Browser`
    to the `Playwright` object that created it (E9). `page` is the only field any caller
    outside this module reads; `_browser`/`_playwright` stay private to this pairing (E9,
    §2.3 -- nothing outside `cua.surface.web` holds a `Browser` or `Playwright` object).
    """

    page: Page
    _browser: Browser
    _playwright: Playwright


def open_session_page(base_url: str, *, headless: bool = True) -> SessionBrowser:
    """Opens one headed-capable Chromium page against `base_url` -- unlike `launch_page`, not
    as a context manager, because a session's page is held across many separate HTTP requests
    by `cua.session.service`, not torn down at the end of the call that opened it.
    `headless=True` is every test's default; a real human handoff needs `headless=False`, set
    by `cua serve`, never by a test.

    **Thread-affine (E15):** the thread that calls this becomes the only thread that may ever
    call a method on the returned `SessionBrowser.page`, or pass it to `close_session_page`,
    for its whole lifetime -- Playwright's sync API is not safe to call from a second OS
    thread against a `Page`/`Browser` a different thread created (verified: it raises
    `greenlet.error`, not a `PlaywrightError`). `cua.session.service`'s session-owning thread
    (Task 6) is this module's only production caller and respects this by construction.
    """
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=headless)
    page = browser.new_page(base_url=base_url)
    return SessionBrowser(page=page, _browser=browser, _playwright=playwright)


def close_session_page(session_browser: SessionBrowser, *, logout_path: str | None = None) -> None:
    """Tears down one session's browser: attempts a logout first (§7.7 -- "attempts", not
    "guarantees"), then closes the page, its browser, and the driver, regardless of whether
    the logout attempt succeeded. Must be called from the same thread that called
    `open_session_page` for this `SessionBrowser` (E15).

    Best-effort by design, like the human-action capture script (E14): a timeout or a
    `PlaywrightError` while navigating to `logout_path` is swallowed, not raised, because a
    teardown that cannot fail to close is the actual safety property here (§7.7: leaving a
    headed browser parked on an authenticated session is the risk this exists to close, and
    that must happen even when the logout attempt itself goes wrong).
    """
    if logout_path is not None:
        with suppress(PlaywrightError):
            session_browser.page.goto(logout_path, timeout=_NAVIGATION_TIMEOUT_MS)
    session_browser.page.close()
    session_browser._browser.close()
    session_browser._playwright.stop()
