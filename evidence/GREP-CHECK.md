# Evidence leak check (phase 8, acceptance criterion 4)

Run on 2026-10-01 after the last discovery run, from the repository root:

    grep -rl '<the mock login password>' evidence artifacts
    grep -rEl '[0-9]{3}-[0-9]{2}-[0-9]{4}' evidence artifacts
    grep -rl 'AI[z]a' evidence artifacts

Result: all three printed nothing. No evidence file or saved artifact contains the login
password, a raw SSN-shaped string, or an API-key prefix. (The pattern `AI[z]a` is written with a bracket so the command does not match this file; this file names the password only
by description, so the first grep does not match itself.)

Limits: the greps read text. Screenshots (PNG) are binary and were not searched; the mock
application renders SSNs already masked (`***-**-7742`), so the screenshots show only that
masked form.

Runs under this directory, oldest first. The earliest ones failed on purpose and are kept
as evidence of what had to be fixed:

- `run-20261001164055-95e0` dead_end: the model was not told the declared inputs existed.
- `run-20261001165243-14bf`, `-165311-5a9e`, `-173200-deb7`, `-175129-0c09`, `-175203-c712`,
  `-180003-6bc7`, `-180458-568d`, `-180559-65a5`: lookup capability failed self-verify
  because compiled locators and matchers pinned the balance read in discovery.
- `run-20261001181359-dc8d`, `run-20261001182355-ba57`: `lookup_member_savings_balance`
  saved, `verified: true` (the second has the full evidence set).
- `run-20261001182526-523c`: `open_subaccount` saved, `verified: true`, stops on the review
  page. The goal text told the model not to press Post, so this run alone does not show
  escalation.
- `run-20261001182637-5949`: same goal as the plan's wording; model stopped at the review
  page and the save was refused because `v1.yaml` already existed.
- `run-20261001182705-3573`: escalation probe. The goal required pressing Post; the loop
  logged `policy_refused` for `click` on `Post` (irreversible, `policy_mode=strict`) and
  stopped, outcome `refused`. The mock application's log shows no `POST /subaccount/post`.
- `run-20261001185502-6ed7`: escalation probe attempt after D62. The model stopped at the review
  page and saved a throwaway artifact (removed; not part of the deliverable). No escalation.
- `run-20261001185531-f1f0`: same probe, Gemini returned HTTP 429, outcome `failed`.
- `run-20261001185610-0bdc`: escalation probe with the D62 escalator. The goal required clicking
  Post; the loop called the escalator at step 10 (`click` on `Post`, irreversible), handback
  `CannotResolve`, outcome `escalated`, nothing saved. The mock application's log shows no
  `POST /subaccount/post`. Text grep of these three runs for the login password, SSN-shaped
  strings and the API-key prefix found nothing.

Notes on specific runs and criteria:

- The runs from `run-20261001164055-95e0` through `run-20261001181359-dc8d` predate D60 and
  have no `run.json` or `result.json`, so they carry no token counts.
- `run-20261001185502-6ed7`: its `result.json` `stop_detail` wrongly claims Post was pressed.
  That is a model hallucination; the trace shows it stopped on the review page. Its artifact
  path points at an artifact that has since been removed, and its `artifact.yaml` remains as
  evidence only.
- Criterion 3 (escalation) rests on `run-20261001185610-0bdc`, not on the run behind the saved
  `open_subaccount` artifact.
- `estimated_cost_usd` is null for `gemini-3.5-flash-lite` because no price is on file. Token
  counts are exact, so the cost half of criterion 5 is not met.
