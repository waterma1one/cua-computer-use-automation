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

from dataclasses import dataclass
from typing import Any, Literal, cast

from playwright.sync_api import Dialog, Frame, Page
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator as PlaywrightLocator

from cua.surface.locators import resolve_against, synthesize
from cua.surface.models import (
    Action,
    ActionKind,
    ActionResult,
    EvidenceFrame,
    Locator,
    Node,
    Observation,
    PreconditionFailed,
    Require,
    Resolution,
    SurfaceSegment,
)
from cua.surface.snapshot import parse_aria_snapshot, scrub_protected_values

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


class StaleObservationError(RuntimeError):
    """Raised by `act_on_index` when `generation` is not the current observation.

    Spec §3.1: without this check, a model acting on an index from an earlier
    observation -- one the page has since changed under -- would click something it
    never actually saw.
    """


@dataclass
class ObservationBudget:
    """Caps how many nodes a single `observe()` call returns."""

    max_nodes: int = 120


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


def _handle_for(frame: Frame, node: Node, nodes: list[Node]) -> PlaywrightLocator:
    """Maps a resolved `Node` to a live Playwright handle for acting on it.

    `get_by_role(role, name=...)` scoped to `frame`, then `.nth(k)` where `k` is `node`'s
    position among the nodes in `nodes` (already scoped to one frame) sharing its role and
    accessible name -- the same identity `resolve_against` used to pick `node` out in the
    first place, just re-expressed as a live locator rather than a data match.
    """
    same_role_and_name = [n for n in nodes if n.role == node.role and n.name == node.name]
    position = next(i for i, n in enumerate(same_role_and_name) if n.index == node.index)
    role = cast(Any, node.role)
    handle = (
        frame.get_by_role(role, name=node.name, exact=True)
        if node.name is not None
        else frame.get_by_role(role)
    )
    return handle.nth(position)


def _failed_live_precondition(
    handle: PlaywrightLocator, require: Require
) -> Literal["visible", "enabled"] | None:
    """Checks `require` against the live handle rather than the snapshot: the
    accessibility snapshot's state coverage is partial, so both checks are re-verified
    against reality before a resolution is reported `unique`."""
    if require.visible and not handle.is_visible():
        return "visible"
    if require.enabled and not handle.is_enabled():
        return "enabled"
    return None


class WebSurface:
    """A `Surface` (see `cua/surface/base.py`) backed by one live Playwright `Page`."""

    def __init__(self, page: Page, budget: ObservationBudget | None = None) -> None:
        self.page = page
        self.budget = budget or ObservationBudget()
        self._generation = 0
        self._last_nodes: list[Node] = []
        self._pending_dialog: Dialog | None = None
        # Registering a listener stops Playwright's default behaviour of auto-dismissing
        # native dialogs, so one raised by the target app (e.g. the `dialog` fault) stays
        # open until `act()` is asked to dismiss it, rather than vanishing unnoticed.
        self.page.on("dialog", self._on_dialog)

    def _on_dialog(self, dialog: Dialog) -> None:
        self._pending_dialog = dialog

    # ---- perception --------------------------------------------------------

    def observe(self) -> Observation:
        nodes: list[Node] = []
        index = 0
        for frame in self.page.frames:
            path = _surface_path_for(frame)
            raw = _snapshot_frame(frame)
            frame_nodes = parse_aria_snapshot(raw, path, start_index=index)
            nodes.extend(frame_nodes)
            index += len(frame_nodes)

        filtered = [n for n in nodes if _is_relevant(n)]
        truncated = len(filtered) > self.budget.max_nodes
        if truncated:
            filtered = filtered[: self.budget.max_nodes]

        self._generation += 1
        self._last_nodes = filtered
        return Observation(generation=self._generation, nodes=filtered, truncated=truncated)

    def capture(self) -> EvidenceFrame:
        try:
            image_png: bytes | None = self.page.screenshot()
        except PlaywrightError:
            image_png = None
        raw = "\n".join(_snapshot_frame(frame) for frame in self.page.frames)
        return EvidenceFrame(
            generation=self._generation,
            image_png=image_png,
            snapshot_yaml=scrub_protected_values(raw),
        )

    # ---- resolution ---------------------------------------------------------

    def _frame_for(self, surface_path: list[SurfaceSegment]) -> Frame:
        if len(surface_path) == 1 and surface_path[0].kind == "window":
            return self.page.main_frame
        if (
            len(surface_path) == 2
            and surface_path[0].kind == "window"
            and surface_path[1].kind == "frame"
        ):
            frame = self.page.frame(name=surface_path[1].name)
            if frame is None:
                raise ValueError(f"no live frame named {surface_path[1].name!r}")
            return frame
        raise ValueError(f"unsupported surface_path shape: {surface_path!r}")

    def resolve(self, locator: Locator) -> Resolution:
        frame = self._frame_for(locator.surface_path)
        raw = _snapshot_frame(frame)
        nodes = parse_aria_snapshot(raw, locator.surface_path, start_index=0)
        result = resolve_against(locator, nodes)
        if result.kind != "unique":
            return result

        handle = _handle_for(frame, result.node, nodes)
        which = _failed_live_precondition(handle, locator.require)
        if which is not None:
            return PreconditionFailed(which=which)
        return result

    # ---- action ---------------------------------------------------------------

    def act(self, action: Action) -> ActionResult:
        if action.kind == "navigate":
            if action.value is None:
                return ActionResult(ok=False, action=action, read_value=None)
            self.page.goto(action.value)
            self.page.wait_for_load_state("networkidle")
            return ActionResult(ok=True, action=action, read_value=None)

        if action.kind == "dismiss_dialog":
            dialog = self._pending_dialog
            if dialog is None:
                return ActionResult(ok=False, action=action, read_value=None)
            dialog.dismiss()
            self._pending_dialog = None
            return ActionResult(ok=True, action=action, read_value=None)

        if action.locator is None:
            return ActionResult(ok=False, action=action, read_value=None)

        result = self.resolve(action.locator)
        if result.kind != "unique":
            return ActionResult(ok=False, action=action, read_value=None)

        frame = self._frame_for(action.locator.surface_path)
        raw = _snapshot_frame(frame)
        nodes = parse_aria_snapshot(raw, action.locator.surface_path, start_index=0)
        handle = _handle_for(frame, result.node, nodes)

        if action.kind == "click":
            handle.click()
            return ActionResult(ok=True, action=action, read_value=None)
        if action.kind == "fill":
            handle.fill(action.value or "")
            return ActionResult(ok=True, action=action, read_value=None)
        if action.kind == "select":
            handle.select_option(action.value)
            return ActionResult(ok=True, action=action, read_value=None)
        if action.kind == "press_key":
            handle.press(action.value or "")
            return ActionResult(ok=True, action=action, read_value=None)
        if action.kind == "wait_for":
            handle.wait_for(state="visible")
            return ActionResult(ok=True, action=action, read_value=None)
        if action.kind == "read":
            return ActionResult(
                ok=True, action=action, read_value=result.node.value or result.node.name
            )

        return ActionResult(ok=False, action=action, read_value=None)

    # ---- discovery-time entry point --------------------------------------------

    def act_on_index(self, generation: int, index: int, action: str) -> ActionResult:
        """The discovery-time counterpart to `act()`: the model names an index from an
        `Observation` it was just handed, rather than building a `Locator` itself.

        `generation` must match the observation this surface most recently produced --
        anything older raises `StaleObservationError` rather than executing (spec §3.1).
        A fresh locator is then synthesized for the named node (`cua.surface.locators.
        synthesize`, the same synthesis phase 3's compiler uses) and executed through
        `act()`, so there is exactly one implementation of "perform an action" underneath
        both entry points.
        """
        if generation != self._generation:
            raise StaleObservationError(
                f"generation {generation} is stale; current generation is "
                f"{self._generation}"
            )
        node = next((n for n in self._last_nodes if n.index == index), None)
        if node is None:
            raise ValueError(f"no node with index {index} in the current observation")
        locator = synthesize(node, self._last_nodes)
        built = Action(kind=cast(ActionKind, action), locator=locator, value=None)
        return self.act(built)
