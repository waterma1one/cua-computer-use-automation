"""Criterion 2 at the surface, against the phase-1 application: an application-initiated
navigation to a denied URL is refused and the session freezes.

Two enforcement points (E2), both live here. Request-stage interception *prevents* what
the page itself initiates (a link, a form, `goto`) -- `LEDGER` proves the mutating GET
never reached the server. `framenavigated` *detects* a server redirect that lands on a
denied URL, which interception cannot see (probed: the 303 hop after `POST /search` is
invisible to the route handler and visible to `framenavigated` before `click()` returns);
that one GET has fired by the time it is detected, and the test says so.

Uses `browser`/`live_mockapp` from `tests/conftest.py`; never `launch_page` (one sync
driver per thread -- the CLI's own live test is in `tests/test_cli.py`).

Live-capture reconciliation (E14): the `/account/000100045512-01` frame is recorded in
a local live-snapshot capture file; the close control is
`link "Close this account"` and its `/url` is `/account/close?number=000100045512-01`.
"""
import contextlib

import pytest

from cua.artifact.validate import DeploymentAllowlist
from cua.policy.allowlist import navigation_guard
from cua.surface.base import Surface, SurfaceError
from cua.surface.models import Action, Locator, SurfaceSegment
from cua.surface.web import ObservationBudget, WebSurface
from mockapp.app import DEFAULT_LOGIN_PASSWORD, DEFAULT_LOGIN_USER, LEDGER

TOP_LEVEL = [SurfaceSegment(kind="window", name="main")]
ALL_ACTIONS = ["navigate", "click", "fill", "select", "press_key", "wait_for", "read",
               "dismiss_dialog"]


def _deployment(origin: str, **over) -> DeploymentAllowlist:
    fields = dict(allowed_origins=[origin], allowed_paths=["/"],
                  denied_paths=["/account/close"], allowed_actions=ALL_ACTIONS)
    fields.update(over)
    return DeploymentAllowlist(**fields)


@pytest.fixture
def page(browser, live_mockapp):
    page = browser.new_page(base_url=live_mockapp)
    yield page
    page.close()


def _login(page) -> None:
    page.goto("/login")
    page.get_by_role("textbox", name="User", exact=True).fill(DEFAULT_LOGIN_USER)
    page.get_by_role("textbox", name="Password", exact=True).fill(DEFAULT_LOGIN_PASSWORD)
    page.get_by_role("button", name="Sign in", exact=True).click()
    page.wait_for_load_state("networkidle")


def _close_link() -> Locator:
    return Locator(role="link", name="Close this account", surface_path=TOP_LEVEL,
                   rationale="the account page's close control", confidence="high")


def test_a_guarded_surface_still_satisfies_the_protocol(page, live_mockapp) -> None:
    surface = WebSurface(page, navigation_guard=navigation_guard(_deployment(live_mockapp)))
    assert isinstance(surface, Surface)
    assert surface.allowlist_violation() is None


def test_a_link_to_a_denied_path_is_aborted_before_the_request_leaves(page, live_mockapp) -> None:
    # Prevention: the mutating GET never reaches the server.
    _login(page)
    page.goto("/account/000100045512-01")
    page.wait_for_load_state("networkidle")
    surface = WebSurface(page, ObservationBudget(max_nodes=200),
                         navigation_guard=navigation_guard(_deployment(live_mockapp)))
    before = len(LEDGER)
    result = surface.act(Action(kind="click", locator=_close_link()))
    assert result.ok  # Playwright's click returns once the initiated navigation fails
    violation = surface.allowlist_violation()
    assert violation is not None
    assert "/account/close" in violation and "deny rules are evaluated first" in violation
    assert len(LEDGER) == before, "the deny rule must stop the mutation, not just report it"


def test_a_frozen_surface_refuses_every_further_act_but_still_captures(page, live_mockapp) -> None:
    _login(page)
    page.goto("/account/000100045512-01")
    page.wait_for_load_state("networkidle")
    surface = WebSurface(page, ObservationBudget(max_nodes=200),
                         navigation_guard=navigation_guard(_deployment(live_mockapp)))
    surface.act(Action(kind="click", locator=_close_link()))
    assert surface.allowlist_violation() is not None
    with pytest.raises(SurfaceError, match="frozen"):
        surface.act(Action(kind="navigate", locator=None, value="/member/12345"))
    with pytest.raises(SurfaceError, match="frozen"):
        surface.act_on_index(1, 0, "click")
    # Evidence of the freeze is still obtainable.
    frame = surface.capture()
    assert frame.image_png is not None
    assert surface.observe().generation >= 1
    assert surface.pending_dialog() is None


def test_a_navigate_to_a_denied_origin_is_refused_at_the_request_stage(page, live_mockapp) -> None:
    # Only the origin differs from the live one; nothing about the mock app is reached.
    guard = navigation_guard(_deployment("http://allowed.example"))
    surface = WebSurface(page, navigation_guard=guard)
    with pytest.raises(SurfaceError):
        surface.act(Action(kind="navigate", locator=None, value="/login"))
    violation = surface.allowlist_violation()
    assert violation is not None and "is not an allowed origin" in violation


def test_a_server_redirect_onto_a_denied_path_is_detected_and_freezes(page, live_mockapp) -> None:
    # Detection, not prevention: `POST /search` 303s to `/member/12345`, and interception
    # cannot see the hop. The GET has fired by the time this is caught -- E2 states the cost.
    _login(page)
    surface = WebSurface(page, ObservationBudget(max_nodes=200),
                         navigation_guard=navigation_guard(
                             _deployment(live_mockapp, denied_paths=["/member/"])))
    member_id = Locator(role="textbox", name="Member ID", surface_path=TOP_LEVEL,
                        rationale="search field", confidence="high")
    search = Locator(role="button", name="Search", surface_path=TOP_LEVEL,
                     rationale="search button", confidence="high")
    assert surface.act(Action(kind="fill", locator=member_id, value="12345")).ok
    assert surface.act(Action(kind="click", locator=search)).ok
    violation = surface.allowlist_violation()
    assert violation is not None and "'/member/12345'" in violation
    with pytest.raises(SurfaceError, match="frozen"):
        surface.act(Action(kind="click", locator=search))


def test_a_child_frame_navigation_is_checked_too(page, live_mockapp) -> None:
    # The framed index loads /nav and /search in child frames (probed). Denying /nav must
    # freeze the surface even though the main frame's URL is permitted.
    _login(page)
    surface = WebSurface(page, navigation_guard=navigation_guard(
        _deployment(live_mockapp, denied_paths=["/nav"])))
    # whether the aborted child frame fails the load is not what this test pins
    with contextlib.suppress(SurfaceError):
        surface.act(Action(kind="navigate", locator=None, value="/"))
    violation = surface.allowlist_violation()
    assert violation is not None and "'/nav'" in violation


def test_a_guard_that_raises_denies_rather_than_permits(page, live_mockapp) -> None:
    def broken(url: str) -> str | None:
        raise RuntimeError("guard exploded")

    surface = WebSurface(page, navigation_guard=broken)
    with pytest.raises(SurfaceError):
        surface.act(Action(kind="navigate", locator=None, value="/login"))
    violation = surface.allowlist_violation()
    assert violation is not None and "guard exploded" in violation


def test_an_unguarded_surface_enforces_nothing(page, live_mockapp) -> None:
    # E4: phase-2/4 fixtures construct `WebSurface(page)`; that stays unenforced. The CLI is
    # what always supplies a guard (Task 6 pins it).
    _login(page)
    surface = WebSurface(page)
    assert surface.act(Action(kind="navigate", locator=None, value="/member/12345")).ok
    assert surface.allowlist_violation() is None


def test_a_violation_reason_never_carries_userinfo(page, live_mockapp) -> None:
    # Controller ruling: the two `_freeze` sentences must render the URL with any userinfo
    # stripped, even though the guard itself still receives (and may echo, in its own
    # reason) the original URL.
    surface = WebSurface(page, navigation_guard=lambda url: "refused")
    with pytest.raises(SurfaceError):
        surface.act(Action(kind="navigate", locator=None, value="http://user:pw@127.0.0.1:1/"))
    violation = surface.allowlist_violation()
    assert violation is not None
    assert "pw" not in violation
    assert "127.0.0.1:1" in violation
