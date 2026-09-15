import socket
import threading
import time

import pytest
import uvicorn

from mockapp.app import create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def live_mockapp():
    port = _free_port()
    config = uvicorn.Config(create_app("base"), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    # E30: module-scoped, not session-scoped -- the driver closes at the end of
    # `tests/surface/test_web_surface.py` rather than staying open for the rest of the
    # session, which is what let a second, independent `sync_playwright()` session
    # (`cua.surface.web.launch_page`, exercised for real by `tests/test_cli.py`) collide
    # with this one: Playwright's sync API allows only one open driver connection per OS
    # thread at a time.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        yield b
        b.close()


@pytest.fixture(scope="module")
def browser_page(browser):
    page = browser.new_page()
    yield page
    page.close()
