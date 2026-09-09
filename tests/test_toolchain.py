import sys
from pathlib import Path


def test_python_version_meets_floor() -> None:
    assert sys.version_info >= (3, 12)


def test_package_imports() -> None:
    import cua

    assert cua is not None


def test_env_example_documents_no_secret_values() -> None:
    text = Path(".env.example").read_text()
    assert "GEMINI_API_KEY=" in text
    for line in text.splitlines():
        if line.startswith("GEMINI_API_KEY="):
            assert line.strip() == "GEMINI_API_KEY=", "the example file must carry no real key"
