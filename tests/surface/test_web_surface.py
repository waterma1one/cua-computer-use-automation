import pytest

from cua.surface.base import SurfaceError
from cua.surface.locators import ancestors_of, synthesize
from cua.surface.models import Action
from cua.surface.snapshot import parse_aria_snapshot
from cua.surface.web import ObservationBudget, StaleObservationError, WebSurface
from mockapp.app import DEFAULT_LOGIN_PASSWORD, DEFAULT_LOGIN_USER


def _login(page, base_url: str) -> None:
    """Logs `page` in against the standalone /login page (not behind any frame)."""
    page.goto(base_url + "/login")
    page.get_by_role("textbox", name="User", exact=True).fill(DEFAULT_LOGIN_USER)
    page.get_by_role("textbox", name="Password", exact=True).fill(DEFAULT_LOGIN_PASSWORD)
    page.get_by_role("button", name="Sign in", exact=True).click()
    page.wait_for_load_state("networkidle")


@pytest.fixture
def surface(live_mockapp, browser_page):  # fixtures defined in tests/conftest.py
    # `/` is behind a login gate: its content frame's src is /search, which requires a
    # session, so an unauthenticated visit renders the Sign in form rather than Member
    # Search. Log in first, on a plain top-level navigation, before going to `/`.
    _login(browser_page, live_mockapp)
    browser_page.goto(live_mockapp + "/")
    browser_page.wait_for_load_state("networkidle")
    return WebSurface(browser_page, ObservationBudget(max_nodes=200))


def test_observe_reaches_every_frame(surface) -> None:
    obs = surface.observe()
    frames = {seg.name for n in obs.nodes for seg in n.surface_path if seg.kind == "frame"}
    assert {"nav", "content"} <= frames


def test_observe_finds_the_search_control(surface) -> None:
    obs = surface.observe()
    assert any(n.role == "button" and n.name == "Search" for n in obs.nodes)


def test_generation_increments_on_each_observation(surface) -> None:
    assert surface.observe().generation < surface.observe().generation


def test_acting_on_a_stale_index_is_refused(surface) -> None:
    first = surface.observe()
    surface.observe()
    target = next(n for n in first.nodes if n.role == "button" and n.name == "Search")
    with pytest.raises(StaleObservationError):
        surface.act_on_index(first.generation, target.index, "click")


def test_a_synthesized_locator_resolves_on_the_live_page(surface) -> None:
    obs = surface.observe()
    target = next(n for n in obs.nodes if n.role == "button" and n.name == "Search")
    loc = synthesize(target, obs.nodes)
    assert surface.resolve(loc).kind == "unique"


# R9 replaces the brief's `test_a_password_field_never_reports_a_value`, which is vacuous:
# its fixture (the search page) has no password field at all, so its generator is empty and
# `all([])` passes -- today, and against an implementation that leaks every password. It is
# also the plan's only discharge of spec §3.7.3, which requires typing a real value into a
# password field and proving it does not leak, end to end, through a real browser. A live
# probe (recorded in the phase-2 ledger) found the leak happens three times over: as the
# textbox's own value, and inside the accessible *names* of its enclosing cell and row --
# so this checks the whole serialized observation, not just the one node.
def test_a_password_field_never_leaks_its_value(live_mockapp, browser_page) -> None:
    sentinel = "xk4q9z7wpm2v"  # >=12 chars, distinctive, not a real-looking credential
    browser_page.goto(live_mockapp + "/login")
    browser_page.get_by_role("textbox", name="Password", exact=True).fill(sentinel)

    surface = WebSurface(browser_page, ObservationBudget(max_nodes=200))
    obs = surface.observe()

    password_nodes = [n for n in obs.nodes if n.name == "Password"]
    assert password_nodes, "expected at least one 'Password'-named node in the observation"
    for node in password_nodes:
        assert node.state.protected is True
        assert node.value is None

    serialized = "".join(str(n.model_dump()) for n in obs.nodes)
    assert sentinel not in serialized

    assert sentinel not in surface.capture().snapshot_yaml


# R7 adds this: the brief's only live resolution test targets the unscoped `Search` button,
# so the scope path -- the marquee hostile case -- never touched a browser in phase 2, even
# though phase 3's compiler is built directly on it. Member 12345 has exactly two accounts,
# so its detail page really does carry two identically-named `Select` buttons. This proves
# scoped resolution end to end, and that it resolves to the *right* button, not merely to
# *something* unique.
def test_a_scoped_locator_resolves_to_the_right_duplicate_named_button(
    live_mockapp, browser_page
) -> None:
    _login(browser_page, live_mockapp)
    browser_page.goto(live_mockapp + "/search")
    browser_page.get_by_role("textbox", name="Member ID", exact=True).fill("12345")
    browser_page.get_by_role("button", name="Search", exact=True).click()
    browser_page.wait_for_load_state("networkidle")

    surface = WebSurface(browser_page, ObservationBudget(max_nodes=200))
    obs = surface.observe()

    select_buttons = [n for n in obs.nodes if n.role == "button" and n.name == "Select"]
    assert len(select_buttons) == 2, "member 12345 must have exactly two accounts"

    def row_and_button(prefix: str):
        row = next(
            n for n in obs.nodes if n.role == "row" and n.name and n.name.startswith(prefix)
        )
        frame_nodes = [n for n in obs.nodes if n.surface_path == row.surface_path]
        button = next(b for b in select_buttons if row in ancestors_of(b, frame_nodes))
        return row, button

    savings_row, savings_select = row_and_button("Savings")
    checking_row, checking_select = row_and_button("Checking")

    savings_loc = synthesize(savings_select, obs.nodes)
    checking_loc = synthesize(checking_select, obs.nodes)
    # R16: the nearest named ancestor of a Select button is its own enclosing cell, which
    # is *also* named "Select" (it contains nothing else) and so cannot disambiguate;
    # `synthesize` must climb past it to the row, which is named "Savings ..." /
    # "Checking ...". This much only proves `synthesize` picked the right scope, not that
    # `resolve()` honours it -- both `result.node.role`/`.name` below are "button"/"Select"
    # for *either* row by construction, so they cannot tell the two resolutions apart, and
    # `result.node.index` is not usable either: it comes from `resolve()`'s own
    # independent, unfiltered re-snapshot of the frame, a different numbering scheme than
    # `observe()`'s filtered-and-renumbered Observation (fix round 1, Important 4).
    assert savings_loc.scope is not None
    assert savings_loc.scope.name is not None and savings_loc.scope.name.startswith("Savings")
    assert checking_loc.scope is not None
    assert checking_loc.scope.name is not None and checking_loc.scope.name.startswith("Checking")

    savings_result = surface.resolve(savings_loc)
    checking_result = surface.resolve(checking_loc)
    assert savings_result.kind == "unique"
    assert checking_result.kind == "unique"

    # Both resolve() calls above independently re-snapshot and re-parse the same,
    # unchanged live page, so -- unlike observe()'s renumbered indices -- their raw index
    # numbering is directly comparable: two parses of identical raw YAML assign identical
    # indices to identical positions. Resolving the two scoped locators to genuinely
    # different buttons must therefore land on genuinely different indices.
    assert savings_result.node.index != checking_result.node.index

    # And each resolved node's enclosing row -- checked with the pure layer's own
    # containment helper (`ancestors_of`), against a snapshot parsed the same way
    # resolve() parses its own, rather than re-deriving containment or reusing observe()'s
    # differently-numbered Observation -- is the row it was scoped to, not the other one.
    frame = browser_page.main_frame
    raw = frame.locator(":root").aria_snapshot()
    fresh_nodes = parse_aria_snapshot(raw, savings_loc.surface_path, start_index=0)

    savings_ancestors = ancestors_of(savings_result.node, fresh_nodes)
    checking_ancestors = ancestors_of(checking_result.node, fresh_nodes)
    assert any(
        a.role == "row" and a.name and a.name.startswith("Savings") for a in savings_ancestors
    )
    assert any(
        a.role == "row" and a.name and a.name.startswith("Checking") for a in checking_ancestors
    )
    assert not any(
        a.role == "row" and a.name and a.name.startswith("Checking") for a in savings_ancestors
    )
    assert not any(
        a.role == "row" and a.name and a.name.startswith("Savings") for a in checking_ancestors
    )


# Fix round 1, Important 1: resolve(...).kind == "unique" alone cannot see a bug in the
# resolved-node-to-live-handle mapping -- a reviewer probe hardcoded that mapping to
# always pick position 0 and every existing test, including the R7 test above, still
# passed, while a click meant for the Checking account's Select button actually landed on
# Savings. This drives the click all the way through both rows and checks the resulting
# page by account number -- a value the wrong button's page could not also produce -- so a
# regression in the handle mapping fails this test even though it would not fail
# `resolve()`-only coverage.
def test_clicking_the_correct_duplicate_named_button_reaches_the_right_account(
    live_mockapp, browser_page
) -> None:
    _login(browser_page, live_mockapp)
    surface = WebSurface(browser_page, ObservationBudget(max_nodes=200))

    for row_prefix, expected_number in (
        ("Savings", "000100045512-01"),
        ("Checking", "000100045512-02"),
    ):
        browser_page.goto(live_mockapp + "/member/12345")
        browser_page.wait_for_load_state("networkidle")

        obs = surface.observe()
        select_buttons = [n for n in obs.nodes if n.role == "button" and n.name == "Select"]
        assert len(select_buttons) == 2

        row = next(
            n for n in obs.nodes if n.role == "row" and n.name and n.name.startswith(row_prefix)
        )
        frame_nodes = [n for n in obs.nodes if n.surface_path == row.surface_path]
        target = next(b for b in select_buttons if row in ancestors_of(b, frame_nodes))

        loc = synthesize(target, obs.nodes)
        result = surface.act(Action(kind="click", locator=loc, value=None))
        browser_page.wait_for_load_state("networkidle")

        assert result.ok is True
        assert expected_number in browser_page.url, (
            f"expected the {row_prefix} account ({expected_number}) after clicking its "
            f"Select button, landed on {browser_page.url!r} instead"
        )


# Fix round 1, Important 2 / R21: the mock app's `?fault=dialog` injects an inline
# `window.confirm(...)` that blocks the page's load event entirely. `act(navigate)` must
# turn that into a fast, honest `ActionResult(ok=False, ...)` naming the pending dialog --
# not hang for the default 30s and then raise a raw Playwright exception past the surface
# boundary, which no caller outside `cua/surface/` is even allowed to import a type to
# catch. Dismissing the dialog and retrying against a non-faulted URL must then succeed.
def test_navigating_into_a_pending_dialog_reports_it_then_recovers(
    live_mockapp, browser_page
) -> None:
    _login(browser_page, live_mockapp)
    surface = WebSurface(browser_page, ObservationBudget(max_nodes=200))

    blocked = surface.act(
        Action(kind="navigate", locator=None, value=live_mockapp + "/search?fault=dialog")
    )
    assert blocked.ok is False
    assert blocked.read_value is not None
    assert "dialog" in blocked.read_value.lower()

    dismissed = surface.act(Action(kind="dismiss_dialog", locator=None, value=None))
    assert dismissed.ok is True

    recovered = surface.act(Action(kind="navigate", locator=None, value=live_mockapp + "/search"))
    assert recovered.ok is True


# Fix round 2, Item 1 (completes R21): `dialog.dismiss()` was the one Playwright call site
# in `act()` left unwrapped. Forcing an already-handled dialog back into the surface's
# private `_pending_dialog` -- the same route used to provoke this live -- must raise
# `SurfaceError`, not the raw Playwright exception type: nothing outside `cua/surface/` is
# allowed to import Playwright to catch that type.
def test_dismiss_dialog_translates_a_playwright_failure_to_surfaceerror(
    live_mockapp, browser_page
) -> None:
    _login(browser_page, live_mockapp)
    surface = WebSurface(browser_page, ObservationBudget(max_nodes=200))

    blocked = surface.act(
        Action(kind="navigate", locator=None, value=live_mockapp + "/search?fault=dialog")
    )
    assert blocked.ok is False

    dialog = surface._pending_dialog
    assert dialog is not None
    dialog.dismiss()  # handle it for real...
    surface._pending_dialog = dialog  # ...then force the surface to think it is still pending

    with pytest.raises(SurfaceError):
        surface.act(Action(kind="dismiss_dialog", locator=None, value=None))


# Fix round 2, Item 2: capture()'s screenshot path used to swallow a PlaywrightError into
# a silent `image_png=None`, while the raw-snapshot path two lines below correctly raised.
# Evidence that could not be captured is not the same as evidence that is absent -- phase
# 4 writes these frames to /evidence/, where a silently-missing screenshot reads as
# "nothing to see" rather than "capture failed". Both paths now raise SurfaceError.
def test_capture_translates_a_screenshot_failure_to_surfaceerror(
    live_mockapp, browser_page, monkeypatch
) -> None:
    from playwright.sync_api import Error as PlaywrightError

    browser_page.goto(live_mockapp + "/login")
    surface = WebSurface(browser_page, ObservationBudget(max_nodes=200))

    def _boom(**kwargs: object) -> bytes:
        raise PlaywrightError("synthetic screenshot failure")

    # Only screenshot() fails; the raw-snapshot path (frame.locator(":root")
    # .aria_snapshot()) is left fully functional, so this isolates the screenshot path
    # specifically rather than failing both at once for an unrelated reason (e.g. a
    # closed page, which would raise from the snapshot path too and pass this test even
    # without the fix).
    monkeypatch.setattr(browser_page, "screenshot", _boom)

    with pytest.raises(SurfaceError):
        surface.capture()


# Fix round 1, Important 3: act_on_index's `action: str` parameter has nowhere to carry a
# value, so silently defaulting one of these to "" or None would perform the wrong action
# (e.g. blank the field) while still reporting ok=True -- the worst outcome this system
# can produce. Each value-requiring kind must be refused instead.
@pytest.mark.parametrize("kind", ["fill", "select", "press_key"])
def test_act_on_index_refuses_kinds_it_cannot_supply_a_value_for(surface, kind: str) -> None:
    obs = surface.observe()
    target = next(n for n in obs.nodes if n.role == "textbox" and n.name == "Member ID")
    result = surface.act_on_index(obs.generation, target.index, kind)
    assert result.ok is False
    assert result.read_value is not None
    assert "act_on_index" in result.read_value


# Fix round 1, Important 4: a live probe on a real multi-frame observation showed indices
# with gaps (filtering happens after index assignment, and nothing renumbered), even
# though Task 2's parser contract -- and the brief's "continuous indices" description of
# observe() -- both mean dense and zero-based.
def test_observation_indices_are_dense_and_zero_based(surface) -> None:
    obs = surface.observe()
    assert [n.index for n in obs.nodes] == list(range(len(obs.nodes)))
