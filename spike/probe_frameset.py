# spike/probe_frameset.py
"""Throwaway probe. Answers the four phase-0 gate questions. Deleted at the end of phase 0."""
from __future__ import annotations

import http.server
import socketserver
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "frameset"
PORT = 8799


def serve() -> socketserver.TCPServer:
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=str(ROOT), **kw
    )
    httpd = socketserver.TCPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main() -> None:
    httpd = serve()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/index.html")
            page.wait_for_load_state("networkidle")

            print("Q1 frames:", [f.name for f in page.frames])
            content = page.frame(name="content")
            print("Q1 frame(name=content) found:", content is not None)
            assert content is not None

            print("\nQ2/Q3 aria snapshot of the content frame:\n")
            print(content.locator("body").aria_snapshot())

            print("\nQ3 disabled control resolvable and reported disabled:")
            post = content.get_by_role("button", name="Post Transfer")
            print("  count:", post.count(), "enabled:", post.is_enabled())

            print("\nQ2 duplicate-name controls (expect 2):")
            print("  count:", content.get_by_role("button", name="Select").count())

            print("\nQ4 password field:")
            content.get_by_role("textbox", name="PIN").fill("hunter2")
            snap = content.locator("body").aria_snapshot()
            print("  PIN node present:", "PIN" in snap)
            print("  VALUE LEAKED:", "hunter2" in snap)
            assert "hunter2" not in snap, (
                "the accessibility snapshot exposes password values; the surface layer must "
                "strip them explicitly rather than relying on the browser"
            )

            browser.close()
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    main()
