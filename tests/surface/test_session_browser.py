"""E9: `open_session_page`/`close_session_page` -- a headed-capable page that outlives the
call that opened it, and a teardown that attempts a logout before closing regardless of
whether the attempt succeeded.
"""
from cua.surface.web import close_session_page, open_session_page
from mockapp.app import DEFAULT_LOGIN_PASSWORD, DEFAULT_LOGIN_USER


def test_a_session_page_outlives_the_call_that_opened_it_and_can_be_driven_normally(
    live_mockapp,
) -> None:
    session_browser = open_session_page(live_mockapp)
    page = session_browser.page
    try:
        page.goto("/login")
        page.get_by_role("textbox", name="User", exact=True).fill(DEFAULT_LOGIN_USER)
        page.get_by_role("textbox", name="Password", exact=True).fill(DEFAULT_LOGIN_PASSWORD)
        page.get_by_role("button", name="Sign in", exact=True).click()
        page.wait_for_load_state("networkidle")
        assert "/search" in page.url
    finally:
        close_session_page(session_browser)


def test_close_attempts_a_logout_and_the_server_side_session_actually_ends(live_mockapp) -> None:
    session_browser = open_session_page(live_mockapp)
    page = session_browser.page
    page.goto("/login")
    page.get_by_role("textbox", name="User", exact=True).fill(DEFAULT_LOGIN_USER)
    page.get_by_role("textbox", name="Password", exact=True).fill(DEFAULT_LOGIN_PASSWORD)
    page.get_by_role("button", name="Sign in", exact=True).click()
    page.wait_for_load_state("networkidle")
    session_cookie = next(c["value"] for c in page.context.cookies() if c["name"] == "session")
    close_session_page(session_browser, logout_path="/logout")
    # The server-side session is gone: a fresh page presenting the same cookie is bounced.
    verifier = open_session_page(live_mockapp)
    try:
        verifier.page.context.add_cookies([{
            "name": "session", "value": session_cookie, "url": live_mockapp,
        }])
        verifier.page.goto("/search")
        assert "/login" in verifier.page.url
    finally:
        close_session_page(verifier)


def test_close_swallows_a_logout_failure_and_still_closes(live_mockapp) -> None:
    session_browser = open_session_page(live_mockapp)
    session_browser.page.goto("/login")
    # A logout_path that does not exist must not raise out of close_session_page.
    close_session_page(session_browser, logout_path="/this-route-does-not-exist")
    assert session_browser.page.is_closed()


def test_close_with_no_logout_path_is_a_bare_close(live_mockapp) -> None:
    session_browser = open_session_page(live_mockapp)
    close_session_page(session_browser)
    assert session_browser.page.is_closed()
