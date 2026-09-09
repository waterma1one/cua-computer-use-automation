from pathlib import Path

import pytest

FORBIDDEN = ("playwright", "selenium")
PURE_PACKAGES = ("artifact", "replay", "policy", "catalog")


def test_only_surface_may_import_a_browser_driver() -> None:
    for package in PURE_PACKAGES:
        root = Path("cua") / package
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            text = path.read_text().lower()
            for name in FORBIDDEN:
                assert f"import {name}" not in text, f"{path} imports {name}"
                assert f"from {name}" not in text, f"{path} imports {name}"


def test_the_boundary_check_can_actually_detect_an_import() -> None:
    # Without this, the test above passes vacuously until the pure packages exist, and a test
    # that cannot fail is not a test. Once phase 2 lands, this asserts the detector works by
    # confirming it sees the one import that is supposed to be there.
    web = Path("cua/surface/web.py")
    if not web.exists():
        pytest.skip("phase 2 has not landed yet")
    assert "playwright" in web.read_text().lower()
