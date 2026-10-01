# REPORT

`cua` turns one LLM-driven exploration of a web application into a reviewed, replayable capability.
Discovery needs a model key; replay, the catalog and the operator console do not. The target is a
local credit-union back office (`mockapp/`): a frameset app with no test IDs, session expiry,
injectable faults, a restricted member, an irreversible Post, and a second tenant variant.
Run evidence is in `evidence/README.md`.

## 1. Architecture

Two processes, and only because one boundary is forced: the brief wants a human to take over the
*same* live session the automation was using, so the session (and the control lease) must outlive any
single replay call. `cua serve` is that session service and the operator console. Everything else is a
library behind one CLI (`discover`, `replay`, `catalog`, `approve`, `serve`). `replay` runs
`embedded` (owns its browser, no escalation: an escalation trigger becomes `ESCALATION_UNAVAILABLE`
instead of hanging) or through the service.

The invariant that matters: `surface/web.py` is the only module that imports Playwright. `artifact/`
and `replay/` speak only in locators and actions, and a test greps for the violation. That is what
makes section 4 a seam rather than an essay.

The perception mechanism is the accessibility tree, not screenshots or DOM selectors. It is the
abstraction a desktop platform also exposes, it is stable across restyling, and it is much cheaper in
tokens. The cost: nodes carry no handles, so the mapping from an observed node to a replayable locator
is our own tested module (`surface/locators`).

The discovery model contributes metadata only. The artifact is compiled mechanically from the
executed action trace, so it is never a model-written summary of its own transcript, and a discovered
artifact is only saved after it replays in a fresh session. Trade-off accepted: a discovered artifact
is minimal. It knows the states it observed, and nothing else.

## 2. Artifact schema

One YAML file per `(id, version)`, immutable once written. Shape: `inputs` and `outputs` (JSON
Schema generated from the Pydantic models, so the catalog exports the same object replay validates
against, with a test asserting identity), ordered `steps`, `success.checkpoint`, `recovery`, and
`provenance` with a pointer to the trace.

- **The error taxonomy is declarative.** Each step carries `expects`: first match by declaration order
  wins, with outcome `continue`, `business` (with a code), `retry` or `fail`. A global `recovery` list
  handles interstitials (`dismiss` or `escalate`). Anything matching neither is a hard failure. Putting
  this in the file, not in Python `try/except`, makes it diffable, reviewable and portable.
- **Immutable contract versus mutable lifecycle.** The file holds what the capability *is*. Status
  (`draft`/`approved`), approver, replay counts and stability score live in `registry.json`, keyed
  `(id, version)`, so approving never edits a reviewed file.
- **No DOM-derived value is storable.** A locator is role plus name (plus `surface_path` for frames),
  with a mandatory `rationale` and `confidence`, because the file is meant to be read by a person.
  Ordinals raise a reviewable warning; no regular expressions anywhere.
- **No host.** Keyed to the vendor product; the base URL and the allowlist are deployment config.
- `expects` carry `source: observed | proposed | authored` and `verified`. An unverified clause blocks
  approval, which is the guard against branches nobody has seen fire.

## 3. Determinism & error handling

No model in the replay loop. No fixed sleeps: every wait is condition polling against a declared
timeout. Per step: resolve, policy check, act, settle, classify. Resolution is strict: unique
proceeds; not found tries fallbacks and then fails; ambiguous tries scope, then ordinal, then
fallbacks, then fails. It never takes the first match, because that is where determinism ends.
`require.visible/enabled` is part of resolution, so a disabled control is not a success.

The result is a three-way split, as values and never exceptions: `Success`, `BusinessOutcome`
(`MEMBER_NOT_FOUND` returns normally, with a code the caller branches on), and `Failure` with a kind
from a closed taxonomy plus mandatory `expected` and `observed`. Inputs are validated before a
browser opens. Outputs are validated against the declared schema, so a bad money parse is
`OUTPUT_VALIDATION_FAILED`, not a silent null.

`NO_BRANCH_MATCHED` is the most useful failure: reality produced a state the author never declared.
It is also the drift signal. Replaying the artifact against variant B *without* its overlay fails
closed at the renamed field (`run-20261001202501-6da2`) rather than guessing. The design expects this
to fire often at first; each one is an intervention, and its resolution becomes an `observed` clause
in version N+1.

The application is treated as an independent actor: the allowlist is enforced on `framenavigated` and
route interception, not only on our `navigate` steps; native dialogs are recorded and routed, never
auto-accepted.

## 4. Heterogeneity & multi-tenant

**Surfaces.** The seam is `Surface` (observe, act, resolve, plus an allowlist-violation query).
`WebSurface` is the one implementation. A legacy web app already works through frame-aware
`surface_path` (the mock is a frameset). A desktop surface would implement `Surface` against an OS
accessibility API, and the recorded flow does not change because it only names roles and names.
`ax_path` locators are expressed in accessibility terms for that reason. Not built.

**Tenants.** A tenant variant is an *overlay* on the base artifact, resolved at load time: it may
override a locator's `name` or `surface_path`, insert steps, skip steps, and extend `expects`. It may
not touch `inputs` or `outputs`, because that would silently break every caller; the validator
rejects it. Demonstrated: variant B renames `Member ID` to `Account Holder ID` and inserts a branch
screen. `artifacts/.../overlays/v2.b.yaml` is 4 small edits, and `cua replay --overlay` returns the
same balance (`run-20261001202459-23c5`). The overlay carries its own `verified` flag, since a verified
base says nothing about whether the overlay resolves on its variant. Drift detection is the
`NO_BRANCH_MATCHED` signal plus the stability score. Not built: overlay discovery (proposing an
overlay from a failed replay) and the tenant registry.

## 5. Escalation & handoff

Triggers, all inside the replay loop: a recovery rule with `handle: escalate`, any failure on a step
classified `irreversible`, and `NO_BRANCH_MATCHED`. The engine pauses and raises an intervention
with the capability, the step, the reason code, expected versus observed, and a screenshot and snapshot.
A human claims it, which moves the lease to `operator`; the automation's own surface is then refused
in code (tested). The operator works the same live browser. Hand back is one of `resolved` (resume
after re-verifying the escalating step's own `continue` clause), `resolved_manually`
(`Success(assistance: human)`), `restart_from` (only if every re-run step is `safe`) or
`cannot_resolve`. State is captured before and after the human window, and an injected script
records clicks and input on a best-effort basis. An unclaimed intervention expires and the session
is torn down with a logout attempt.

What the lease binds, stated honestly: it is an agent-side mutex and an audit record. It stops the
automation from acting under a person; it does not stop the person acting. The operator console is
a read-only view with token auth, and claim and hand back go through the JSON API (D69); the human's hands are the one mocked piece in the demo
(`scripts/handoff_demo.py`). After any handback the run reports
`assistance: human` and is excluded from the stability score (D70; the committed escalation evidence
predates this and still records `none`).

## 6. Safety

- **Allowlist** is per-deployment config, never the artifact. Deny rules win, because legacy apps
  mutate on GET (`/account/close` sits inside an allowed prefix). A capability policy can narrow the
  deployment allowlist and never widen it.
- **Risk classes** `safe`, `risky`, `irreversible`. Risky needs `approved`. Irreversible needs
  `approved`, `confirm_irreversible`, and an idempotency key. Discovery under `strict` policy
  escalates at the Post and never performs it (`run-20261001185610-0bdc`).
- **Controls that bind.** `confirm_irreversible` is a flag the calling agent sets, so it is not the
  control. The catalog exposes an irreversible capability as a *request*: invoking it creates an
  intervention and executes nothing. The approval gate (`draft` to `approved`, never automatic) is the
  real control.
- **Secrets.** Credentials are placeholders to the model, never values. Protected-field values are
  stripped when a snapshot is parsed, so they never exist in an observation. A pattern filter covers
  SSN, card and account shapes on every evidence write, and `redact: true` masks an output in
  evidence while still returning it to the caller. A leak check over `evidence/` and `artifacts/` is
  recorded in `evidence/GREP-CHECK.md`.
- **Limits.** Failure screenshots can contain PII; production needs retention limits and visual
  masking, and PNGs were not searched. The risk heuristic over-classifies on purpose and will
  misclassify, so the human approval is the control. The idempotency ledger protects *our* side only
  (in-memory, 24 h): it cannot make a legacy application idempotent. Prompt injection is bounded
  structurally by the closed action set and the allowlist, not by an instruction. The default model
  tier may retain data, acceptable only because the target data is synthetic; the artifact records
  `provider_retention`. A `safe` read can still trigger a server-side effect on a mutating GET; deny
  rules cover known cases only. The `[REDACTED]` marker is applied by global substring replacement.

## 7. Cuts

Cut, in the order agreed up front:

- **Assisted LLM fallback** (phase 10): a replay failure goes to a human or a failure, never to a
  model. It was the first thing cut.
- **Stability scoring** is built (only unassisted successes count) but uninformative against a
  deterministic mock; it needs real drift to mean anything.
- Discovery emits no outputs or business clauses (D53). The v2 artifact used for replay evidence is
  hand-authored on top of the discovered v1 (D68), and says so.
- No cost figure for discovery: no price is on file for the model. Token counts are exact.
- Desktop surface, multi-tenant plumbing, cross-process idempotency, and the console's TLS and buttons are
  designed, not built. The console's `?token=` bootstrap is acceptable on loopback only.
- The unfinished edges are listed in `docs/context/DECISIONS.md` (D44, D52).

Next, in order: persist idempotency and the registry; make `resolved` handbacks count as assisted;
let discovery propose outputs and business clauses; generate an overlay from a failed replay; then a
desktop `Surface`.
