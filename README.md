# cua

Record-once, replay-many automation for applications with no API. A model explores a web
application once; the executed trace is compiled into a reviewed capability artifact; replay then
runs that artifact with no model, with typed inputs and outputs, a three-way result
(`Success`, `BusinessOutcome`, `Failure`), and a human-escalation path that takes over the live
session. The design write-up is `REPORT.md`; run evidence is `evidence/README.md`; the decision log
is `docs/context/DECISIONS.md`.

## Setup

Python 3.12+.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/playwright install chromium
cp .env.example .env     # only `cua discover` needs GEMINI_API_KEY filled in
```

`cua` below is `.venv/bin/cua`. Checks (all clean on a fresh clone):

```bash
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/mypy cua mockapp
```

### Without live services

Only `cua discover` calls a model and needs a key and network. Replay, the catalog, approval and the
operator console need neither: the target is a local mock app, and `evidence/` plus `artifacts/`
already hold a discovered artifact to replay.

## Demo path

Start both mock variants in two terminals (see below), then, from the repository root:

```bash
# 1. Discover (needs GEMINI_API_KEY): the model drives a real browser, the trace is compiled to an
#    artifact and replayed in a fresh session before it is saved as a draft. See "Discovery".
# 2. Approve v2 of the saved capability. A human act, never automatic; the catalog refuses drafts.
cua approve mockcu.lookup_member_savings_balance 2 --root . --approver <your-name>

# 3. Replay it, no model: typed inputs in, typed outputs out.
cua replay mockcu.lookup_member_savings_balance 2 --root . --evidence-root . \
  --base-url http://127.0.0.1:8811 --policy policy.demo-a.yaml \
  --input username=teller --input password=teller-demo-pw --input member_id=12345
#   -> {"outputs":{"balance":"4218.60"}, ...}

# 4. A business outcome is a result, not a crash (exit 0): member 00000 does not exist.
cua replay mockcu.lookup_member_savings_balance 2 --root . --evidence-root . \
  --base-url http://127.0.0.1:8811 --policy policy.demo-a.yaml \
  --input username=teller --input password=teller-demo-pw --input member_id=00000
#   -> {"code":"MEMBER_NOT_FOUND", ...}

# 5. The same artifact on tenant variant B (renamed field, extra branch screen), via an overlay.
cua replay mockcu.lookup_member_savings_balance 2 --root . --evidence-root . \
  --base-url http://127.0.0.1:8812 --policy policy.demo-b.yaml \
  --overlay artifacts/mockcu.lookup_member_savings_balance/overlays/v2.b.yaml \
  --input username=teller --input password=teller-demo-pw --input member_id=12345
#   Without --overlay the same command fails closed: NO_BRANCH_MATCHED at s4.

# 6. Escalation: a wrong password stalls the run, a human takes the live session and hands it back.
PYTHONPATH=. .venv/bin/python scripts/handoff_demo.py --policy policy.demo-a.yaml
```

Step 6 drives the production session service through its HTTP routes. `cua serve` is the interactive
form: a headed browser the operator uses, with claim and hand back through the JSON API
(`POST /interventions/{id}/claim`, `/handback`). The script scripts only the operator's hands.
Every replay writes `evidence/<run_id>/`.

## Target mock application

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

## Discovery (the only step that needs a key)

`cua discover` is the only command that needs a model API key (`GEMINI_API_KEY` in `.env`).
Replay, the catalog and the operator console run without one.

Start the mock app (above), then copy `policy.example.yaml` to a scratch file, change its
origin to `http://127.0.0.1:8811` and keep `policy_mode: strict`. From the repository root:

```bash
set -a; . ./.env; set +a
cua discover \
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
replay-only. The escalation demonstration is `run-20261001185610-0bdc`. `evidence/GREP-CHECK.md`
records the leak check and lists every run, failures included.

## Catalog demo path

Saved artifacts are discoverable and gated by approval. None of these commands needs an API key.

```bash
cua catalog list --root .          # every capability with its status (draft or approved)
cua approve mockcu.lookup_member_savings_balance 2 --root . --approver <your-name>
cua catalog invoke mockcu.lookup_member_savings_balance --root . \
  --base-url http://127.0.0.1:8811 --policy policy.demo-a.yaml \
  --input username=teller --input password=teller-demo-pw --input member_id=12345
```

`catalog invoke` refuses a draft (exit 2, the message names `cua approve`). An irreversible
capability is never executed: an intervention request is printed and the exit code is 1. The
idempotency ledger (24h) and interventions are in-memory and per process, and the ledger
protects our side only. Only unassisted successful runs count toward the stability score.
