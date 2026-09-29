import pytest

from cua.surface.base import StaleObservationError, Surface, SurfaceError
from cua.surface.locators import ancestors_of, synthesize
from cua.surface.models import Action, Locator, SurfaceSegment
from cua.surface.snapshot import parse_aria_snapshot
from cua.surface.web import ObservationBudget, WebSurface
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


@pytest.fixture
def surface_low_budget(live_mockapp, browser_page):
    # Deferred-minor 6: a budget tight enough that naive positional slicing (page.frames
    # enumerates nav before content) would return only nav's branding chrome.
    _login(browser_page, live_mockapp)
    browser_page.goto(live_mockapp + "/")
    browser_page.wait_for_load_state("networkidle")
    return WebSurface(browser_page, ObservationBudget(max_nodes=5))


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


def test_act_on_index_fills_when_given_a_value(surface) -> None:
    obs = surface.observe()
    target = next(n for n in obs.nodes if n.role == "textbox" and n.name == "Member ID")
    result = surface.act_on_index(obs.generation, target.index, "fill", value="12345")
    assert result.ok
    assert result.action.value == "12345"
    assert result.action.locator is not None


def test_act_on_index_still_refuses_fill_with_no_value(surface) -> None:
    obs = surface.observe()
    target = next(n for n in obs.nodes if n.role == "textbox" and n.name == "Member ID")
    result = surface.act_on_index(obs.generation, target.index, "fill")
    assert result.ok is False
    assert "act_on_index" in (result.read_value or "")


def test_expand_widens_the_budget_and_reobserves(surface) -> None:
    small = WebSurface(surface.page, budget=ObservationBudget(max_nodes=1))
    first = small.observe()
    assert first.truncated is True
    expanded = small.expand()
    assert expanded.generation != first.generation
    assert len(expanded.nodes) >= len(first.nodes)


def test_raw_snapshot_returns_the_unfiltered_population(surface) -> None:
    obs = surface.observe()
    raw = surface.raw_snapshot()
    assert len(raw) >= len(obs.nodes)
    # every model-facing node's identity is present among the raw population
    raw_ids = {(n.role, n.name, tuple((s.kind, s.name) for s in n.surface_path)) for n in raw}
    for n in obs.nodes:
        assert (n.role, n.name, tuple((s.kind, s.name) for s in n.surface_path)) in raw_ids


def test_raw_snapshot_before_any_observe_is_empty(browser) -> None:
    fresh = WebSurface(browser.new_page())
    assert fresh.raw_snapshot() == []


# Fix round 1, Important 4: a live probe on a real multi-frame observation showed indices
# with gaps (filtering happens after index assignment, and nothing renumbered), even
# though Task 2's parser contract -- and the brief's "continuous indices" description of
# observe() -- both mean dense and zero-based.
def test_observation_indices_are_dense_and_zero_based(surface) -> None:
    obs = surface.observe()
    assert [n.index for n in obs.nodes] == list(range(len(obs.nodes)))


# CRITICAL 1: `_handle_for` computed a node's position among nodes matching its own
# role/name (nameless, for `search.html`'s unnamed "note" input), but built the live handle
# with no name filter at all -- which matches named and nameless nodes alike. `.nth(position)`
# then indexed into a different, larger population than the one the position was computed
# in, and silently landed on the Member ID field instead. This drives a real `fill` at the
# unnamed input and asserts BOTH that it received the value AND that Member ID stayed empty
# -- asserting only the first half would also pass against a bug that fills every matching
# textbox.
def test_acting_on_the_unnamed_input_never_touches_the_member_id_field(surface) -> None:
    obs = surface.observe()
    target = next(n for n in obs.nodes if n.role == "textbox" and n.name is None)
    loc = synthesize(target, obs.nodes)
    # The pinned unnamed-input hostile case (phase 1): empty at observation time (no name,
    # no value yet), so synthesis has nothing to identify it by but role/position --
    # `ax_path`, spec 3.4 rule 2's explicit last resort -- not `role_name` (would fabricate
    # a role-based name-match that doesn't exist) and not yet `text` (there is no text to
    # match until something is typed).
    assert loc.strategy != "role_name"

    result = surface.act(Action(kind="fill", locator=loc, value="ZZZPROBE"))
    assert result.ok is True

    content = surface.page.frame(name="content")
    unnamed_value = content.get_by_role("textbox", name="", exact=True).input_value()
    member_id_value = content.get_by_role(
        "textbox", name="Member ID", exact=True
    ).input_value()

    assert unnamed_value == "ZZZPROBE"
    assert member_id_value == ""


# CRITICAL 1, via the discovery-time entry point: `act_on_index(gen, index, "click")` on the
# unnamed input must focus the unnamed input, not the Member ID field. Checked by the DOM
# `name` attribute (`note` vs `mid`), independent of accessible-name ambiguity.
def test_act_on_index_on_the_unnamed_input_focuses_the_unnamed_field(surface) -> None:
    obs = surface.observe()
    target = next(n for n in obs.nodes if n.role == "textbox" and n.name is None)
    result = surface.act_on_index(obs.generation, target.index, "click")
    assert result.ok is True

    focused_name = surface.page.frame(name="content").evaluate(
        "() => document.activeElement && document.activeElement.name"
    )
    assert focused_name == "note"


# CRITICAL 2: `_handle_for` mapped every strategy through `frame.get_by_role`, but
# `get_by_role("text")` matches nothing in Playwright -- there is no such queryable role --
# so a `text`-strategy locator (spec 3.4's escape hatch for a control with no accessible
# name worth relying on) could never resolve through `WebSurface` at all, and the empty
# handle's `is_visible()` reported `False`, misdiagnosing a present, unique, visible node as
# failing a visibility precondition. `get_by_text` is the Playwright counterpart. This
# resolves AND acts (via `read`) on a `text`-strategy locator against a real page -- nothing
# in the suite touched this path before.
def test_text_strategy_locator_resolves_and_reads_through_the_web_surface(browser_page) -> None:
    browser_page.set_content("<body>Transfer posted successfully<button>OK</button></body>")
    surface = WebSurface(browser_page, ObservationBudget(max_nodes=200))
    obs = surface.observe()

    target = next(n for n in obs.nodes if n.role == "text")
    loc = synthesize(target, obs.nodes)
    assert loc.strategy == "text"

    resolved = surface.resolve(loc)
    assert resolved.kind == "unique"

    result = surface.act(Action(kind="read", locator=loc, value=None))
    assert result.ok is True
    assert result.read_value == "Transfer posted successfully"


# CRITICAL 2, general case: if a strategy's resolved node cannot actually be mapped to a
# live handle, the honest answer is `not_found`, never a `precondition_failed` inferred from
# an empty handle. Forces `get_by_role` to return an always-empty locator group so the
# resolved node (real, unique, visible) cannot be mapped to anything live, and checks the
# result is `not_found` -- not `precondition_failed(visible)`.
def test_an_unmappable_resolved_node_reports_not_found_not_precondition_failed(
    surface, monkeypatch
) -> None:
    obs = surface.observe()
    target = next(n for n in obs.nodes if n.role == "button" and n.name == "Search")
    loc = synthesize(target, obs.nodes)

    content = surface.page.frame(name="content")
    empty = content.get_by_role("heading", name="nothing matches this, ever")
    monkeypatch.setattr(content, "get_by_role", lambda *a, **kw: empty)

    result = surface.resolve(loc)
    assert result.kind == "not_found"


# I2: `act_on_index` used to synthesize against `self._last_nodes` -- the filtered,
# budget-truncated list the model was shown -- rather than the raw, unfiltered population.
# With a budget tight enough to cut the Checking row (and its Select button) out of the
# model-facing Observation entirely, the Savings Select button looked globally unique to
# synthesis (only one "Select"-named button survived truncation), so it was synthesized with
# no scope at all. `act()` then re-resolves against a fresh, unfiltered snapshot -- which
# still has both buttons -- and that unscoped locator resolves `ambiguous`, failing an
# action for a node that was genuinely, uniquely nameable by index. The budget below was
# picked empirically to admit the Savings row and button but not the Checking row.
def test_act_on_index_synthesizes_against_the_full_population_not_the_truncated_view(
    live_mockapp, browser_page
) -> None:
    _login(browser_page, live_mockapp)
    browser_page.goto(live_mockapp + "/member/12345")
    browser_page.wait_for_load_state("networkidle")

    surface = WebSurface(browser_page, ObservationBudget(max_nodes=11))
    obs = surface.observe()
    assert obs.truncated is True
    select_buttons = [n for n in obs.nodes if n.role == "button" and n.name == "Select"]
    assert len(select_buttons) == 1, "the budget must cut off the second Select button"

    target = select_buttons[0]
    result = surface.act_on_index(obs.generation, target.index, "click")
    browser_page.wait_for_load_state("networkidle")

    assert result.ok is True
    assert "000100045512-01" in browser_page.url, (
        "the truncated view must not stop synthesis from finding the real scope that "
        "disambiguates the Savings account"
    )


# I3: spec 3.6's observation budget was entirely untested -- `_is_relevant -> return True`
# (no filtering) and `truncated = False` (never truncates) both survived the whole suite.
# This pins filtering: a bare, unlabelled structural wrapper (table/rowgroup/document) must
# not appear in the Observation, while a genuinely interactive, labelled control still does.
def test_observation_filters_out_unlabelled_structural_nodes(surface) -> None:
    obs = surface.observe()
    assert not any(
        n.role in {"table", "rowgroup", "document"} and n.name is None and n.value is None
        for n in obs.nodes
    )
    assert any(n.role == "button" and n.name == "Search" for n in obs.nodes)


# I3: a small budget must set `truncated` and actually clip the node count.
def test_a_small_budget_truncates_and_flags_it(live_mockapp, browser_page) -> None:
    _login(browser_page, live_mockapp)
    browser_page.goto(live_mockapp + "/")
    browser_page.wait_for_load_state("networkidle")

    surface = WebSurface(browser_page, ObservationBudget(max_nodes=3))
    obs = surface.observe()
    assert obs.truncated is True
    assert len(obs.nodes) == 3


# Deferred-minor 6, promoted: `page.frames` enumerates `nav` before `content`, so a naive
# positional slice of a small budget returns only the nav frame's branding chrome and drops
# the entire content frame -- verified live. Truncation must be frame-fair: every frame
# should still contribute nodes under a tight budget, not just the first one enumerated.
def test_truncation_is_frame_fair_not_naive_positional_slicing(surface_low_budget) -> None:
    obs = surface_low_budget.observe()
    assert obs.truncated is True
    frames = {seg.name for n in obs.nodes for seg in n.surface_path if seg.kind == "frame"}
    assert "content" in frames, "a tight budget must not drop the content frame entirely"


# I4: `act(read)` returned `result.node.value or result.node.name`, so an empty field
# silently reported its own accessible name as if it were the field's contents. `read` is
# how phase 4 will read application state for an `expects` checkpoint; an empty field
# reporting its own label as its value is a silently wrong read.
def test_read_on_an_empty_field_returns_none_not_the_accessible_name(surface) -> None:
    obs = surface.observe()
    target = next(n for n in obs.nodes if n.role == "textbox" and n.name == "Member ID")
    loc = synthesize(target, obs.nodes)

    result = surface.act(Action(kind="read", locator=loc, value=None))
    assert result.ok is True
    assert result.read_value is None


def test_read_on_a_filled_field_returns_the_typed_value(surface) -> None:
    obs = surface.observe()
    target = next(n for n in obs.nodes if n.role == "textbox" and n.name == "Member ID")
    loc = synthesize(target, obs.nodes)

    fill_result = surface.act(Action(kind="fill", locator=loc, value="12345"))
    assert fill_result.ok is True

    result = surface.act(Action(kind="read", locator=loc, value=None))
    assert result.ok is True
    assert result.read_value == "12345"


def test_read_on_a_filled_protected_field_never_returns_its_value_or_its_label(
    live_mockapp, browser_page
) -> None:
    browser_page.goto(live_mockapp + "/login")
    surface = WebSurface(browser_page, ObservationBudget(max_nodes=200))
    obs = surface.observe()
    target = next(n for n in obs.nodes if n.role == "textbox" and n.name == "Password")
    loc = synthesize(target, obs.nodes)

    fill_result = surface.act(Action(kind="fill", locator=loc, value="hunter2"))
    assert fill_result.ok is True

    result = surface.act(Action(kind="read", locator=loc, value=None))
    assert result.ok is True
    assert result.read_value is None
    assert result.read_value != "Password"


# I5: `_frame_for` raised a bare `builtins.ValueError` when a named frame no longer exists.
# R21's invariant is that a caller which may not import Playwright has an importable type to
# catch, and a frame disappearing after a navigation is routine in a frameset app -- this is
# the better answer for that case: `not_found`, the same outcome vocabulary already used for
# "the target locator's node is not there," reached through a fresh navigation that
# dismantles the frameset entirely.
def test_resolving_against_a_frame_that_no_longer_exists_reports_not_found(
    live_mockapp, browser_page
) -> None:
    _login(browser_page, live_mockapp)
    browser_page.goto(live_mockapp + "/")
    browser_page.wait_for_load_state("networkidle")

    surface = WebSurface(browser_page, ObservationBudget(max_nodes=200))
    obs = surface.observe()
    target = next(
        n
        for n in obs.nodes
        if n.role == "button"
        and n.name == "Search"
        and any(s.kind == "frame" and s.name == "content" for s in n.surface_path)
    )
    loc = synthesize(target, obs.nodes)

    # Navigate the top-level page clean away from the frameset -- the "content" frame this
    # locator's surface_path names no longer exists at all.
    browser_page.goto(live_mockapp + "/search")
    browser_page.wait_for_load_state("networkidle")

    result = surface.resolve(loc)
    assert result.kind == "not_found"


# I5, the other half: a surface_path shape this surface does not know how to interpret at
# all is a genuine implementation-level anomaly, not something a frameset navigation can
# produce -- that case still raises `SurfaceError`, translated from the bare `ValueError`
# `_frame_for` used to raise directly.
def test_an_unsupported_surface_path_shape_raises_surfaceerror(
    live_mockapp, browser_page
) -> None:
    _login(browser_page, live_mockapp)
    browser_page.goto(live_mockapp + "/")
    browser_page.wait_for_load_state("networkidle")
    surface = WebSurface(browser_page, ObservationBudget(max_nodes=200))

    bogus = Locator(
        strategy="role_name", role="button", name="Search",
        surface_path=[
            SurfaceSegment(kind="frame", name="content"),
            SurfaceSegment(kind="frame", name="nav"),
        ],
        rationale="malformed on purpose", confidence="low",
    )
    with pytest.raises(SurfaceError):
        surface.resolve(bogus)


# I6: R24's live half. `require.visible` is checked only against the live handle, because a
# snapshot can go stale between observation and action. Hides a resolvable, previously-visible
# control after observation and confirms `resolve()` reports `precondition_failed(visible)`.
def test_a_control_hidden_after_observation_reports_precondition_failed_visible(
    live_mockapp, browser_page
) -> None:
    _login(browser_page, live_mockapp)
    browser_page.goto(live_mockapp + "/search")
    browser_page.wait_for_load_state("networkidle")

    surface = WebSurface(browser_page, ObservationBudget(max_nodes=200))
    obs = surface.observe()
    target = next(n for n in obs.nodes if n.role == "button" and n.name == "Search")
    loc = synthesize(target, obs.nodes)

    # `display:none`/`visibility:hidden` remove the element from the accessibility tree
    # entirely (verified empirically), which would make this `not_found`, not the
    # precondition failure this test targets. `transform: scale(0)` shrinks the element's
    # box to nothing -- Playwright's `is_visible()` reports `False` -- while the element
    # stays in the accessibility tree, so `resolve_against` still finds it.
    browser_page.get_by_role("button", name="Search", exact=True).evaluate(
        "el => el.style.transform = 'scale(0)'"
    )

    result = surface.resolve(loc)
    assert result.kind == "precondition_failed"
    assert result.which == "visible"


# I7: nothing pinned that `WebSurface` actually satisfies `Surface` at all. `Surface` is
# `runtime_checkable` (base.py) specifically so this isinstance check is meaningful; a
# structural drift (a renamed or removed method) fails this test even though nothing here
# ever names `WebSurface` in a type annotation.
def test_web_surface_conforms_to_the_surface_protocol_at_runtime(browser_page) -> None:
    surface: Surface = WebSurface(browser_page)
    assert isinstance(surface, Surface)


# Also fix: `capture()` before any `observe()` used to return `generation=0`, numerically
# indistinguishable from a real first generation if that numbering scheme ever changed.
# Real generations are always >= 1 (spec 3.1); the never-observed state now uses a sentinel
# that is unambiguous regardless of numbering scheme.
def test_capture_before_any_observation_reports_a_distinguishable_generation(
    browser_page,
) -> None:
    surface = WebSurface(browser_page)
    frame = surface.capture()
    assert frame.generation < 0
    assert frame.generation != surface.observe().generation
