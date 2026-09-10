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

## Running the target mock application

`mockapp/` is the target application the automation system in `cua/` drives. It ships
as two variants of the same synthetic credit-union back office -- same routes, different
branding and labels -- so cross-tenant reuse has a second real target to run against.
Each variant serves on its own fixed port and needs no environment variable set; every
config knob (login credentials, session expiry budget, fault injection) already has a
working default (see `.env.example`).

Start variant A (default) and variant B in separate terminals:

```bash
.venv/bin/python -m mockapp base   # http://127.0.0.1:8811
.venv/bin/python -m mockapp b      # http://127.0.0.1:8812
```

Open `http://127.0.0.1:8811/` in a browser: the page is a classic frameset. The content
frame asks for a login first (`teller` / `teller-demo-pw`, the synthetic credentials in
`.env.example`); after signing in, search for member `12345` to see its detail page, and
`http://127.0.0.1:8811/search?fault=expired` shows the deterministic session-expiry
screen. Variant B behaves the same way on 8812, with its own branding and an extra
branch-selection step in the search flow.

Stop either with Ctrl-C. If a port is already taken, the runner prints one line naming
the port and variant and exits instead of starting a server that silently serves nothing.

No API key or network access is required to run either variant.
