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
.venv/bin/mypy cua mockapp
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
The same applies to anything other than `base` or `b`: `python -m mockapp <typo>` prints
one line naming what you typed and the valid variants, and exits, instead of crashing.

No API key or network access is required to run either variant.

## Discovery demo path

`cua discover` is the only command that needs a model API key (`GEMINI_API_KEY` in `.env`).
Replay, the catalog and the operator console run without one.

Start the mock app (above), then copy `policy.example.yaml` to a scratch file, change its
origin to `http://127.0.0.1:8811` and keep `policy_mode: strict`. From the repository root:

```bash
set -a; . ./.env; set +a
PYTHONPATH=. .venv/bin/python -c "from cua.cli import app; app()" discover \
  --goal "Log in using the declared username and password inputs, then look up the member whose id is the declared member_id input and read that member's current savings balance." \
  --root . --base-url http://127.0.0.1:8811 --policy <policy-file> --evidence-root . \
  --id mockcu.lookup_member_savings_balance --name "Look up member savings balance" \
  --input username=teller --secret-input password=teller-demo-pw --input member_id=12345 \
  --verify-input member_id=22222
```

The model sees `{{password}}`-style placeholders, never the secret. The artifact is compiled
from the executed trace, then replayed in a fresh session with the `--verify-input` values; it
is saved as `artifacts/<id>/v1.yaml` only if that replay passes. Evidence goes to
`evidence/<run_id>/` (`run.json` with token counts, `result.json`, `artifact.yaml`,
`trace.jsonl`, screenshots, snapshots). A goal that needs the irreversible Post is escalated
under `strict` policy: discovery holds the action, records the escalation (event, frame,
`result.json`), never performs it, and the run ends with outcome `escalated` and nothing saved.
The discovery escalator is non-interactive and declines; a real operator handback is
replay-only. `evidence/GREP-CHECK.md`
records the leak check and lists every run, failures included.
