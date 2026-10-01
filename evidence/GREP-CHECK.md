# Evidence leak check (phase 8, acceptance criterion 4)

Run on 2026-10-01 after the last discovery run, from the repository root:

    grep -rl '<the mock login password>' evidence artifacts
    grep -rEl '[0-9]{3}-[0-9]{2}-[0-9]{4}' evidence artifacts
    grep -rl 'AIza' evidence artifacts

Result: all three printed nothing. No evidence file or saved artifact contains the login
password, a raw SSN-shaped string, or an API-key prefix. (This file names the password only
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
