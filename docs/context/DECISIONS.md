# Decision log

Every non-trivial decision, with the reason. Source material for REPORT.md.

## D1 — Target application: purpose-built hostile legacy web app (local)
A small mock credit-union back-office: framesets/iframes, table-based layout,
server-rendered, no test IDs, no semantic markup.
**Why:** the brief's environment is explicitly legacy surfaces with no clean DOM. A
public demo site with clean test IDs would make the locator-robustness and error-handling
sections theoretical. Owning the app also lets us inject the exact runtime conditions the
brief grades on (validation error, record not found, permission denied, session timeout,
unexpected dialog, slow load) and lets a reviewer run everything offline.
**Cost accepted:** we build the target app as well as the system.

## D2 — Perception and action: accessibility tree with semantic locators
The model observes a flattened accessibility tree (role, accessible name, value, frame
path) with per-node handles, and acts by node handle. Recorded locators are semantic
(role + name + frame path + ordinal), re-resolved fresh on every replay.
**Why:** works on framesets and table soup where CSS selectors do not; it is the same
abstraction desktop platforms expose, so the heterogeneity answer in REPORT.md §4 is a
real seam rather than an essay; and it is far cheaper in tokens than screenshots.
**Rejected:** screenshot + coordinates (brittle recorded coordinates, expensive, still
needs a second locator representation); raw DOM/CSS selectors (contradicts the stated
environment, no path to desktop).

## D3 — Language and runtime: Python, Playwright, Pydantic
**Why:** the artifact schema is the graded centerpiece; Pydantic gives typed models,
validation of replay inputs and outputs, and JSON Schema export, which lets the same
artifact double as an agent-callable tool definition. Playwright Python has full
accessibility-tree and CDP support. Anthropic SDK is first-class.

## D4 — Human handoff: a session service owns the browser; agent and operator both attach
A long-lived session process launches a headed browser, exposes a CDP endpoint, and holds
an explicit control lease (`agent` | `operator` | `none`). On escalation the agent
releases the lease; a minimal operator console shows goal, current step, screenshot and
reason, with Take Control / Hand Back; the human drives the same window while a CDP
listener records their actions into the run log; handback restores the lease and the run
resumes from the paused step.
**Why:** the brief requires control of the *same* live session and asks us to model who is
in control. An explicit lease makes that a first-class concept and leaves a clean seam for
a real remote operator console later.
**Rejected:** in-process pause (control ownership stays implicit); containerized noVNC
(infrastructure the brief tells us not to build).

## D5 — Stretch goals: four selected, tiered by depth
- Tier 1, fully built: agent-facing capability catalog; cross-tenant reuse against a
  second app variant with per-variant overrides.
- Tier 2, cheap and real: approval gating (`draft` | `approved`) plus a replay stability
  score; unattended replay refuses `draft`.
- Tier 3, deliberately fenced: assisted fallback — off by default, single step only,
  allowlist-checked, never on risky actions, recorded as evidence, and the replay result
  is marked as assisted so it is never reported as a clean deterministic replay.
**Why tiered:** the brief says at most one or two stretch goals and does not reward
breadth. Tiering keeps all four while making the depth ordering explicit, and the fence
on assisted fallback protects the "no LLM in the decision loop" property that replay is
evaluated on.

## D6 — Corrections from design review (pre-implementation)
- The package module for run logging is `cua/observability/`, not `cua/evidence/`, because
  `/evidence/` at the repository root is a required deliverable and the names would collide.
- Perception uses `locator.aria_snapshot()` per frame rather than the legacy
  `page.accessibility.snapshot()`. Aria snapshots return no element handles, so the surface
  layer parses the snapshot, assigns its own stable node indices, and synthesizes a locator
  (role + accessible name + frame path + ordinal) for each node. Actions execute through
  `get_by_role(role, name=...)` scoped to the resolved frame.
- Frame addressing is a frame path resolved through `page.frames` / `page.frame(name=...)`,
  not `frame_locator()`. `frame_locator()` targets `<iframe>` elements, while the mock app
  deliberately uses a classic `<frameset>` layout.
- Locator synthesis and frameset resolution are spiked before the artifact schema is
  written, because the schema's locator representation depends on what the surface can
  actually produce.

## D7 — Mock application must be built to exercise the graded paths
The target app is specified to make each required behaviour demonstrable rather than
theoretical: it carries fake regulated-looking data (account numbers, SSN-shaped values,
balances) so redaction has something to redact; it contains at least one irreversible
action so the risky-action policy is exercised rather than asserted; and it exposes
deterministic fault injection for every runtime condition the brief names (validation
error, record not found, permission denied, unexpected dialog, session timeout, slow or
failed load) so evidence runs are reproducible.

## D8 — LLM provider: Gemini free tier by default, behind a thin client
The discovery loop talks to an `LLMClient` interface with a single operation: given
messages and tool schemas, return either a tool call or a completion. Gemini's free tier
is the default because it is free, hosted, requires no local hardware, and has reliable
native function calling; Anthropic and a local Ollama model are one-line swaps.
**Why an abstraction at all:** the loop's only requirement of the model is structured tool
calls, so the interface is roughly thirty lines and it removes provider choice from the
list of things that could block the one discovery run that must be real.
**Note:** free-tier quotas change; confirm current limits at build time rather than
trusting design-time assumptions.

## D9 — Corrections from the full cross-section review
- Every locator carries `rationale` and `confidence`. The brief asks explicitly for the
  reasoning about how each control is identified, and reviewability is a stated goal of the
  artifact; a locator without a stated reason cannot be reviewed.
- Discovery stopping conditions are explicit: max steps, max wall-clock duration, repeated
  observation digest (dead-end detection), and consecutive tool-call failures. Each records
  a distinct termination reason.
- Artifacts are verified before they are saved. The compiler re-resolves each synthesized
  locator against the captured observation, adding `scope` then `ordinal` until it is
  unique, and the compiled artifact is then self-verified by a single replay. Only a
  verified artifact reaches disk.
- One assistance field replaces two flags: `assistance: none | llm_fallback | human`. Only
  `none` counts toward the stability score.
- Two action vocabularies. Discovery actions (`expand`, `finish`, `give_up`) are never
  recorded and cannot appear in an artifact. Replay actions are the closed recorded set;
  `assert_checkpoint` is removed because an `expects` clause with `outcome: continue` is
  the checkpoint, and `wait_for` survives only as a deliberate wait, distinct from the
  automatic settle loop.
- `into` may bind a declared output or a local value prefixed with `_`; locals are used for
  step-to-step chaining and never returned to the caller.
- Inputs carry a `sensitive` flag. Evidence redacts sensitive inputs only; blanket input
  redaction would make failures undebuggable.
- `restart_from` on handback is permitted only when every step in the replayed range is
  classified `safe`.
- `name_match` supports `exact | contains | prefix`. Regular expressions are excluded: they
  are a poor fit for a file meant to be human-audited and an unnecessary risk.
- Redaction preserves value shape (for example `***-**-1234`) so that mismatches remain
  debuggable after redaction.
- Approval is a deliberate manual action (`cua approve <id> <version>`). Every artifact
  produced in this project is discovered under `policy_mode: sandbox` and is therefore
  never auto-approvable by design.
- The stability score is reported honestly: against a deterministic mock application it is
  uninformative, and it only becomes meaningful against real drift.
- Agreed cut order if time tightens: assisted fallback first, then stability scoring, both
  documented in REPORT.md as designed but not built. The capability catalog and cross-tenant
  reuse are not cut.

## D10 — Outcome states are matched as text, not by ARIA role
The specification's earlier examples matched business outcomes with `{role: alert, ...}` and
recovery conditions with `{role: dialog|heading, ...}`. The target application deliberately
carries no ARIA roles, because a real legacy application carries none; adding them purely so our
own locators had something convenient to match would have quietly made the target easier than
the environment the design claims to address. Outcome and recovery states are therefore matched
with the `text` locator strategy, which also exercises that code path rather than leaving it
theoretical. Controls, which browsers do assign implicit roles to (`textbox`, `button`, `link`,
`cell`, `row`), continue to use `role_name`.
