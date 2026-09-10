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

## D11 — Phase 0 spike findings

Ran `spike/probe_frameset.py` (`.venv/bin/python spike/probe_frameset.py`) against
`tests/fixtures/frameset/` served over `http://127.0.0.1:8799/`. Literal output, unedited:

```
127.0.0.1 - - [09/Sep/2026 22:10:56] "GET /index.html HTTP/1.1" 200 -
127.0.0.1 - - [09/Sep/2026 22:10:56] "GET /nav.html HTTP/1.1" 200 -
127.0.0.1 - - [09/Sep/2026 22:10:56] "GET /content.html HTTP/1.1" 200 -
Q1 frames: ['', 'nav', 'content']
Q1 frame(name=content) found: True

Q2/Q3 aria snapshot of the content frame:

- table:
  - rowgroup:
    - row "Member Search":
      - cell "Member Search"
    - row "Member ID":
      - cell "Member ID"
      - cell:
        - textbox "Member ID"
    - row "Search":
      - cell "Search":
        - button "Search"
- table:
  - rowgroup:
    - row:
      - cell:
        - textbox
- table:
  - rowgroup:
    - row "Savings 1234.56 Select":
      - cell "Savings"
      - cell "1234.56"
      - cell "Select":
        - button "Select"
    - row "Checking 78.90 Select":
      - cell "Checking"
      - cell "78.90"
      - cell "Select":
        - button "Select"
- link "Print statement":
  - /url: "#"
  - img "Print statement"
- button "Post Transfer" [disabled]
- textbox "PIN"

Q3 disabled control resolvable and reported disabled:
  count: 1 enabled: False

Q2 duplicate-name controls (expect 2):
  count: 2

Q4 password field:
  PIN node present: True
  VALUE LEAKED: True
Traceback (most recent call last):
  File "spike/probe_frameset.py", line 65, in <module>
    main()
    ~~~~^^
  File "spike/probe_frameset.py", line 54, in main
    assert "hunter2" not in snap, (
           ^^^^^^^^^^^^^^^^^^^^^
AssertionError: the accessibility snapshot exposes password values; the surface layer must strip
them explicitly rather than relying on the browser
```

**Q1 — frame reach and addressing by name: PASS.** `page.frames` lists `['', 'nav',
'content']` (the empty-name entry is the top-level frameset document itself), and
`page.frame(name="content")` resolves to a real frame object.

**Q2 — `aria_snapshot()` returns the fixture's controls: PASS.** The snapshot surfaces the
named textbox (`Member ID`), the unnamed adversarial textbox (bare `textbox`, no accessible
name — present but anonymous, which the design must account for), both identically-named
`Select` buttons (both appear, distinguishable only by ordinal/row context, exactly the
adversarial case the fixture was built for), the icon-only `Print statement` control (named
correctly via its `title` attribute, exposed as a `link` containing an `img` with that
accessible name), and `Post Transfer`.

**Q3 — node state exposure: PASS.** The disabled control is marked twice over: the snapshot
itself renders `button "Post Transfer" [disabled]`, and `locator.is_enabled()` independently
reports `False`. Either signal alone would satisfy the design's `require.enabled`
predicate. A textbox's `value` was not separately exercised as a pre-filled attribute in
this fixture (the PIN field starts empty), but see Q4 below for what the snapshot does and
does not report about typed content.

**Q4 — password distinguishability and value non-leak: FAIL.** The password input is
distinguishable — it appears in the snapshot as `textbox "PIN"` (Chromium's aria snapshot
does not report a distinct `role=textbox` variant for `type=password`; it is only
identifiable by its accessible name, not by role or type). The critical half of the
question is a hard fail: after `content.get_by_role("textbox", name="PIN").fill("hunter2")`,
the *second* `aria_snapshot()` call **does** contain the literal typed value. The probe's
own boolean check reports `VALUE LEAKED: True`, and the guarding `assert "hunter2" not in
snap` raises `AssertionError`. Playwright's `aria_snapshot()` on a Chromium `input
type="password"` includes the current value in the accessible-tree text — the browser does
not redact it the way it redacts rendered glyphs on screen. (The failing assertion also
means the literal YAML text of that second, value-bearing snapshot was not printed by the
probe — only the two boolean lines and the assertion traceback above are the recorded
evidence; the probe was not modified to print it, per instructions not to adjust the probe
to chase more information once the gate had already failed.)

**Gate verdict: FAIL.** Three of four questions pass outright, but Q4 fails on the exact
point flagged as "the important half": the accessibility snapshot leaks a password value
verbatim, so `locator.aria_snapshot()` cannot be trusted as-is to keep sensitive input out of
recorded evidence. The design as it stands (spec §3.2/§3.3) cannot rely solely on the browser
to omit password values from the perception layer; any component that redacts sensitive
values (the evidence writer, the perception layer, or both) must do so explicitly by
filtering known-sensitive fields before the snapshot text is stored or logged, rather than
assuming Chromium's `aria_snapshot()` does this for us. Per the brief, the documented
fallback (CDP `Accessibility.getFullAXTree`) is not evaluated here — that decision belongs to
the controller design, not to this spike.

### Model provider smoke test (`spike/probe_llm.py`)

Ran `.venv/bin/python spike/probe_llm.py` (`google-genai` 2.22.0, installed in `.venv`) against
`GEMINI_API_KEY` from the shell environment. Literal output, unedited except that the traceback
below contains no secret material to begin with (checked programmatically before recording):

```
Traceback (most recent call last):
  ...
  File ".../.venv/lib/python3.14/site-packages/google/genai/errors.py", line 202, in raise_error
    raise ClientError(status_code, response_json, response)
google.genai.errors.ClientError: 401 UNAUTHENTICATED. {'error': {'code': 401, 'message': 'Request
had invalid authentication credentials. Expected OAuth 2 access token, login cookie or other
valid authentication credential. See https://developers.google.com/identity/sign-in/web/devconsole-project.',
'status': 'UNAUTHENTICATED', 'details': [{'@type': 'type.googleapis.com/google.rpc.ErrorInfo',
'reason': 'ACCESS_TOKEN_TYPE_UNSUPPORTED', 'metadata': {'method':
'google.ai.generativelanguage.v1beta.GenerativeService.GenerateContent', 'service':
'generativelanguage.googleapis.com'}}]}}
```

**Credential shape.** `GEMINI_API_KEY` is 53 characters, does not start with `AIza` (the standard
Google AI Studio API key prefix), and is structured as two dot-separated segments starting `AQ...`
— not a bare API key. A preliminary `client.models.list()` call, made only to characterize
authentication before spending a generation call, failed with the identical
`401 / ACCESS_TOKEN_TYPE_UNSUPPORTED` error. The credential in the environment is some kind of
OAuth-style access token, not a Gemini Developer API key, and the `generativelanguage.googleapis.com`
REST endpoint used by `client.models.generate_content(...)` / `client.models.list()` when the
`google-genai` `Client` is constructed with `api_key=...` does not accept it. No alternative auth
mechanism (service account, ADC, Vertex AI project/location credentials) was attempted — out of
scope per the brief.

**Documentation check (performed before running, per the brief's warning that the design-time
snippet may be stale).** Fetched `ai.google.dev/gemini-api/docs/models` and
`ai.google.dev/gemini-api/docs/pricing` directly:
- The design-time snippet's model, `gemini-2.0-flash`, is listed as shut down. The cheapest
  current stable model is `gemini-2.5-flash-lite` ($0.10/$0.40 per million input/output tokens),
  confirmed consistently across the models page and the pricing page. `spike/probe_llm.py` was
  updated to use `gemini-2.5-flash-lite`.
- The call shape in the design-time snippet — `client.models.generate_content(model=..., contents=...,
  config=types.GenerateContentConfig(tools=[types.Tool(function_declarations=[...])]))` — is still
  current and matches the installed SDK's live signature
  (`google.genai.models.Models.generate_content`), so no correction was needed there.
- One correction to the design-time snippet's *extraction* code: rather than manually walking
  `response.candidates[i].content.parts`, use the SDK's own `response.function_calls` property
  (`google.genai.types.GenerateContentResponse.function_calls`), which returns `None` safely when
  candidates/content/parts are absent instead of raising. `spike/probe_llm.py` uses this form.
- A newer `client.interactions.create(...)` surface also exists on the installed SDK (2.22.0) and
  appeared in one documentation fetch, but `generate_content` remains supported and is what the
  probe uses; adopting `interactions` was treated as an unforced, unverified deviation and skipped.

**Outcome: the API call did not succeed.** The probe never reached the point of proving a
structured tool call because authentication itself failed. This is a credential problem, not a
model-identifier or call-shape problem: everything downstream of auth (model choice, call shape,
tool-call extraction) was confirmed against current documentation and is believed correct, but is
**unverified end-to-end** because no request with this credential has ever returned a 200 from
`generativelanguage.googleapis.com`.

**Recommendation for phase 7.** Before phase 7, obtain a genuine Gemini Developer API key (starts
`AIza...`, issued from Google AI Studio / a Google Cloud API-keys page) and export it as
`GEMINI_API_KEY`, or reconfigure `spike/probe_llm.py`'s replacement to authenticate against Vertex
AI (`genai.Client(vertexai=True, project=..., location=...)`) using the OAuth-style credential
already present, if that is in fact a Vertex-scoped token — that determination was out of scope
here. Do not assume the call shape recorded above is end-to-end proven; re-run the equivalent of
this probe with working credentials as the first step of phase 7, not as an afterthought.

## D12 — Outcome classification keys on page text, never on status code
The mock app carries the same declared business outcome at two different status codes,
depending on which path produced it: an unknown member is `200` via `POST /search`
(a POST-redirect-GET landing back on the search page with an error message) but `404`
via `GET /member/{id}`; a restricted member is `200` via search but `403` via detail.
The final whole-branch review flagged this as an inconsistency and asked for a ruling.
**Ruling: keep the behavior, document the rule.** The search page's `200` is realistic
legacy behavior -- a form re-render carrying an inline error is exactly what a
server-rendered POST-redirect-GET flow looks like -- and changing it to match the detail
routes would rewrite already-tested Task 2 outcomes for no gain.
**The rule:** outcome classification keys on page text, never on status code, because the
same declared business outcome legitimately arrives with different statuses depending on
the path that produced it. This also matches how the automation actually perceives the
page: through the accessibility snapshot (page content), not the response line.
**Cost accepted:** if a later phase wants status-code-based classification, it must
revisit this decision explicitly rather than assume one status per outcome.
