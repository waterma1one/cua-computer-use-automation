# cua

Record-once, replay-many automation for applications with no API.

This repository is in early spike. See `docs/specs/` and `docs/plans/` for the design
specification, decision log, and phased implementation plan.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/playwright install chromium
```

Run the checks:

```bash
.venv/bin/pytest -v
.venv/bin/ruff check .
.venv/bin/mypy cua
```
