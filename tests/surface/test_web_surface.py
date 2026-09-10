import pytest

from cua.surface.locators import ancestors_of, synthesize
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

    savings_row = next(
        n for n in obs.nodes if n.role == "row" and n.name and n.name.startswith("Savings")
    )
    frame_nodes = [n for n in obs.nodes if n.surface_path == savings_row.surface_path]
    savings_select = next(
        button
        for button in select_buttons
        if savings_row in ancestors_of(button, frame_nodes)
    )

    loc = synthesize(savings_select, obs.nodes)
    result = surface.resolve(loc)
    assert result.kind == "unique"
    assert result.node.index == savings_select.index
