# Evidence

Every directory is `run-<timestamp>-<id>/` with `run.json` (inputs, sensitive ones masked),
`result.json`, `trace.jsonl`, `screenshots/`, `snapshots/`, and `artifact.yaml` when a run executed
one. Saved artifacts are in `../artifacts/`. `GREP-CHECK.md` lists every discovery run, failed ones
included, and records the leak check.

## The five runs

| What it shows | Run | Outcome |
| --- | --- | --- |
| Real LLM-driven discovery (`gemini-3.5-flash-lite`, 9,257 tokens, 6 steps) | `run-20261001182355-ba57` | `saved`, self-verified, `mockcu.lookup_member_savings_balance` v1 |
| Clean deterministic replay, no model | `run-20261001202456-4349` | `Success`, `balance = 4218.60` |
| Business outcome, not a failure | `run-20261001202458-1c6f` | `BusinessOutcome MEMBER_NOT_FOUND` (member `00000`) |
| Escalation resolved by a human on the same live session | `run-20261001202345-e892` | stalled at s4 (wrong password), operator claimed, signed in, handed back `resolved`, run finished `Success` |
| Variant B replay through a tenant overlay | `run-20261001202459-23c5` | `Success`, steps `s6a`, `s6b` inserted by the overlay |

Supporting runs:

- `run-20261001202501-6da2`: the same variant B replay without the overlay. It fails closed with
  `NO_BRANCH_MATCHED` at s4 (`Member ID` is `Account Holder ID` on B). That is the drift signal.
- `run-20261001202307-6feb`: the first handoff attempt. The operator filled the password but not the
  user name, the sign-in failed again, a second intervention opened and expired unclaimed:
  `ESCALATION_TIMEOUT`. Kept because it shows the unclaimed-escalation path.
- `run-20261001185610-0bdc`: discovery escalation. The goal needed the irreversible Post; discovery
  held the action, never performed it, and ended `escalated` with nothing saved.

## Honest limits of this evidence

- v2 of the artifact is hand-edited from the discovered v1 (see D68). Discovery itself produced v1.
- The human in the escalation run is a script (`scripts/handoff_demo.py`). The service, engine, lease,
  HTTP routes and browser are the production ones; only the operator's hands are scripted. In `cua serve`
  the operator uses a headed browser.
- The handoff run's result says `assistance: none` although a human acted. This is a known gap (D66):
  a `resolved` handback resumes automatically, and only `resolved_manually` is marked `human`.
- No cost figure: no price is on file for the model (token counts are exact).
