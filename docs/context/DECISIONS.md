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

## D13 — The locator vocabulary enforces its rules in the model, not by convention
`Locator` and `Node` are Pydantic models that make the spec's §3.4 and §3.7 rules
unexpressible rather than merely discouraged. `name_match` is a `Literal` with no `regex`
member, so a regular expression cannot enter an artifact. `rationale` and `confidence` are
required fields, so a locator that cannot be reviewed by a human cannot be constructed. A
validator rejects a `Node` whose state is `protected` while it carries a value.
**`Resolution` is a discriminated union on a literal `kind` field** — `unique`, `not_found`,
`ambiguous`, `precondition_failed` — because two later modules read those strings.
**`Locator.strategy`** is `Literal["role_name", "text", "ax_path"]`, defaulting to `role_name`.
**Cost accepted:** the model layer carries logic, so a future consumer that wants a
relaxed locator must relax the model explicitly and in public, which is the intent.

## D14 — `Locator.role` is optional, and required only for the `role_name` strategy
A `text`-strategy locator exists precisely to match a control with no accessible name worth
relying on, in an application that carries no ARIA roles at all. Requiring a role there forces
the synthesizer to fabricate one, and the fabricated value lands in an artifact a human is
meant to review. `role` is therefore `str | None`, with a validator requiring it to be
non-None when `strategy == "role_name"` — the invariant that actually matters, since a
`role_name` locator without a role is meaningless.
**Cost accepted:** a later phase that assumed a non-None role must check; the validator
confines that to the non-`role_name` strategies, which such a phase must special-case anyway.

## D15 — Protection is inferred from the accessible name, and the value is scrubbed from ancestors too
Nothing in an accessibility snapshot marks a field as a password field. `parse_aria_snapshot`
receives only YAML text, so the node's accessible name is the only signal available, and it is
matched **on word boundaries** against a documented, extensible tuple of credential tokens —
word boundaries because naive substring matching makes "shipping" and "spinner" protected.

More importantly: **stripping the protected node's own value does not stop the leak.** A live
probe of the target application showed a typed password appearing three times in one snapshot —
as the textbox's value, and inside the accessible **names** of the enclosing cell and row,
because an accessible name is computed from descendant content and therefore propagates a
secret upward. Spec §3.7.1 as literally written ("strip the value of any node whose state
includes protected") does not achieve what §3.7.3 asserts. The rule is widened: the parser
strips the value and scrubs that string out of the node's own name and its ancestors' names,
ancestors being identified through the `depth` chain. Ancestor-scoped rather than a global
sweep, so a short password cannot blank unrelated text elsewhere on the page.
**Known blind spot, recorded deliberately:** a password field with an unusual label, or with
**no** label at all, is not detected. The target application carries an unnamed input as a
required hazard, so this is a real shape, not a hypothetical one.
**Cost accepted:** a credential reaching the model is the worst failure available to this
system, so the test that proves the property asserts the sentinel is absent from the entire
serialized observation, not merely from the protected node.

## D16 — `Node.depth` carries tree structure through a flat list
Observations are a flat list, but `scope` means "inside the Savings row" and secret scrubbing
means "this node's ancestors". Both need containment. Rather than parent pointers, each `Node`
carries `depth`, the accessibility-tree nesting level, and containment is derived by walking
backward keeping a running minimum. **One shared helper serves every call site** — synthesis,
resolution, and the secret scrub — because if any two disagreed about what "inside the Savings
row" means, the system would silently click the wrong control or leak a value it believed it
had scrubbed. Ordering is a load-bearing precondition: a caller passing a reordered or filtered
list gets wrong ancestors back, not an error, and the helper says so at the point of risk.
**Cost accepted:** a deeply nested duplicate could pick a too-distant scope; the ordinal
fallback bounds the damage and the locator's `confidence` reports it.

## D17 — Resolution exhausts the fallback chain before reporting any failure
Spec §3.4 rule 3 requires that an ambiguous match try fallbacks rather than fail immediately,
and that resolution never take a first match. Resolution therefore runs in two passes: try the
primary and then every fallback in order, returning the first that resolves uniquely; only if
none does, report the first non-`not_found` outcome in attempt order, or a composite
`NotFound` naming every attempt. Attempt order rather than a severity ranking, because the
fallback list is already the author's stated order of preference and a second ordering rule is
one more thing a human reviewing an artifact must hold in their head.
**A fallback must carry the same `surface_path` as the locator it backs**, enforced in the
model. Identity and uniqueness are scoped per surface path, so a fallback resolving in a
different frame reopens exactly the ambiguity that scoping exists to close — and it does so on
the recovery path, where a human is least likely to be watching.
**Cost accepted:** a genuine case for a cross-frame fallback must relax the validator
explicitly, and the relaxation is then reviewable rather than accidental.

## D18 — `ax_path` is a nested scope chain, not a new field
Spec §3.4 describes `ax_path` as "an ordered path of `role[name]` segments from a stable
ancestor". `Locator.scope` is itself a `Locator` and nests, so the chain is already
expressible. Adding a `segments` field would create a second way to say the same thing, and
one more shape for the compiler and for a human reviewer to understand.
**Cost accepted:** a deep path serializes as a nested object rather than a flat list.

## D19 — Playwright fetches and acts; it never decides which node a locator means
`WebSurface.resolve()` takes a fresh snapshot of the target frame, parses it, and calls the
pure `resolve_against`. On a unique result it maps the node to a live handle and checks
`require` against that handle. The alternative — resolving with `get_by_role(...)` directly —
would reimplement scope, ordinal, `name_match` and containment in a second place, across a
language boundary where a disagreement with the pure layer is hardest to see. One matching
engine, in the layer that is pure and tested.
**Corollary, learned the hard way:** mapping a node to a handle is itself a place identity can
be lost. The population a position is computed in must be the population the handle indexes
into, and a strategy this surface cannot address must report `NotFound` saying so — never a
precondition failure inferred from an empty locator, which reports "hidden" when the truth is
"unaddressable".
**Cost accepted:** resolution costs one extra snapshot per call.

## D20 — No raw driver exception crosses the surface boundary
Phases 3 through 6 may not import Playwright, so a Playwright exception escaping `web.py` is
literally uncatchable where it needs catching. Surface-owned exception types live in the
import-clean `base.py`, and every failure inside the concrete surface — driver errors and the
surface's own, such as a named frame that no longer exists — is translated before it crosses.
A pending dialog is a reported outcome with a short explicit timeout, not a thirty-second hang
followed by a driver timeout.
**Cost accepted:** an exception hierarchy in `base.py` that a future desktop surface must also
raise, which is what a protocol module is for.

## D21 — Visibility is a live-only precondition
The pure layer cannot know whether a node is visible: a node's presence in a snapshot is the
only signal available offline. The live surface can and must check it, because a snapshot can
go stale between observation and action. Both halves are correct and neither is redundant, so
each module documents its own half and points at the other.
**Cost accepted:** offline replay through the pure layer checks `enabled` but not `visible`,
which a later phase relying on offline resolution must know.

## D22 — The surface protocol carries the stale-generation promise, and the path vocabulary admits panes
`act_on_index` is a member of the `Surface` protocol, not an implementation detail of the web
surface: rejecting an action that references an index from an earlier observation is a promise
the abstraction makes (spec §3.1), and leaving it off the protocol would force the discovery
loop to type against the concrete Playwright-importing class. `SurfaceSegment.kind` admits
`pane` alongside `window` and `frame`, because spec §3.3 states the desktop path is window
then pane, and a closed literal without it is the one place the seam's own portability claim
fails.
**Cost accepted:** a future desktop surface must implement an index-based entry point, and one
literal value is unused until a desktop surface exists.

## D23 — The discovery-only action vocabulary belongs to the discovery loop, not the surface
Spec §3.5 names `expand`, `finish` and `give_up` as discovery-only actions, and §3.6 says the
model uses `expand` when the observation budget has elided something. These are promises the
discovery loop makes, not the surface, and they are deferred to the phase that builds it. The
surface still reports `truncated` honestly and degrades every frame fairly rather than dropping
whichever frame the driver happens to enumerate last.
**Cost accepted:** the discovery phase may add a second action enumeration rather than
extending the recorded one — which is correct anyway, since the recorded set is closed so that
the policy engine can classify risk per action type.

## D24 — The model id is configuration, and D11's diagnosis was wrong on both counts
D11 recorded two conclusions, and live verification has now falsified both.

**It said the credential was the problem.** The key in the environment is `AQ.`-prefixed,
and D11 concluded that shape was an OAuth-style token the Gemini Developer API would not
accept, on the evidence of a 401 `ACCESS_TOKEN_TYPE_UNSUPPORTED`. A key of that shape,
passed as `X-goog-api-key`, now returns `HTTP 200` from `GET /v1beta/models`, generates
content, and honours a `responseSchema` with an enum — all three verified directly. Whatever
produced that 401, the prefix was not it. The rule this leaves behind: diagnose a credential
failure by the endpoint that is failing, not by the shape of the string.

**It said `gemini-2.5-flash-lite` was the confirmed replacement.** That id now returns
`404 NOT_FOUND`: *"no longer available to new users. Please update your code to use
models/gemini-3.5-flash-lite."* So the replacement for a shut-down model was itself
superseded between the spike and the phase that would have used it, while still appearing
in `GET /v1beta/models` — the listing shows it, `generateContent` refuses it.

**Decision.** The model id lives in `.env` as `CUA_LLM_MODEL`, defaulting to
`gemini-3.5-flash-lite`, never as a constant in code. Two ids have now gone out from under
this project inside one build; a third will. The discovery client must also fail usefully:
when `generateContent` returns 404, report the configured id **and** the ids that currently
support `generateContent`, because the provider's own list endpoint disagrees with its
generate endpoint and a bare 404 sends the reader to the wrong question entirely.

**Cost accepted:** a run can be pointed at a model nobody tested. The verification step in
phase 7 is what catches that, and the error message is what makes it a ten-second fix.

## D25 — A `when` clause is a `Matcher`, not a `Locator`
§4.1 writes `expects[].when`, `recovery[].detect` and `success.checkpoint` as
`{ role: heading, name: "Member Search" }` — no `surface_path`, no `rationale`, no
`confidence`, all three of which D13 makes `Locator` require. A predicate over what is on
screen is not an instruction to act on one control and has no business carrying a resolution
strategy's apparatus. `Matcher` holds `strategy`, `role`, `name`, `name_match` and nothing
else, reusing the surface layer's literals so the vocabulary cannot drift.
**Cost accepted:** two shapes for "which node" — one to act on, one to check — and a reader
has to know which is which.

## D26 — The artifact's on-disk shape is the reviewable deliverable, so the model bends to it
Three rulings share one reason: a human reviews the YAML file, so its shape is the spec's
§4.1 shape and the Pydantic layer adapts rather than the other way round.
- `value` is discriminated by which key is present (`{from_input: x}`, `{literal: "..."}`,
  `{from_step: s3}`), not by a `kind:` tag. Three single-field classes, presence
  discriminates, and `{from_input: x, literal: y}` is rejected rather than resolved to
  whichever arm matched first.
- `extra="forbid"` on every artifact model. `Target(path="/x", host="evil.example.com")`
  parsed cleanly with `host` silently discarded — the exact leak `Target` exists to prevent,
  failing quietly. An artifact is authored, reviewed and hand-edited by people; a key that
  vanishes without complaint is a defect the reviewer cannot see. `schema_version` is the
  deliberate decision point for readers meeting a shape they do not understand.
- `save` dumps with `exclude_none=True` and `sort_keys=False`, so the file carries §4.1's
  worked example shape in §4.1's field order, with no `pattern: null` litter.
**Cost accepted:** a future field addition makes older readers reject newer artifacts,
which is what `schema_version` is for.

## D27 — Every check that cannot run says so; nothing is inert or silent
- §4.4's seventh condition ("policy narrows, never widens, the deployment allowlist")
  cannot be checked against a deployment that does not exist until phase 5.
  `validate(artifact, deployment=None)` skips it and emits a `note`-level
  `ALLOWLIST_NOT_CHECKED`, so a caller can never read "not checked" as "checked and clean".
  The same shape recurs when the content scan cannot serialise a cyclic locator graph: it
  emits `FORBIDDEN_CONTENT_NOT_CHECKED` beside the cycle error rather than passing silently.
  `load` returns `(artifact, findings)` unconditionally — an opt-in `return_findings` made
  the default call shape hand back a bare `Artifact` that looked fully checked, and its union
  return broke `mypy --strict` at every call site. A pair cannot be not-noticed.
- A `locator_overrides` entry naming a step with no locator is an error-level finding.
  It produced zero findings and left the step unchanged: an explicit instruction that does
  nothing, with no diagnostic, is worse than a hard failure because the author believes it
  worked. Same through-line as `DUPLICATE_STEP_ID`, `LOCATOR_FALLBACK_CYCLE` (a cyclic
  chain had validated clean and was silently pruned at replay), and `RISK_UNCLASSIFIED` as
  an error rather than a `safe` default.
- A `LiteralValue` on a step with no locator is an error-level `LITERAL_WITHOUT_LOCATOR`.
  The credential check inspects the locator's name, so "no locator to check" and "checked,
  found nothing" had shared the same empty return, and a `press_key` step carrying a
  password literal saved verbatim. Every value-consuming replay action acts on a resolved
  handle, so the step was unreplayable as well as uncheckable.
**Cost accepted:** phase 9's catalog must gate on the note, and every `load` call site
unpacks a tuple. Both are the point.

## D28 — One security-relevant rule has exactly one implementation
`is_protected_name` moved beside `PROTECTED_NAME_TOKENS` in `cua/surface/models.py`
because two layers — the snapshot parser inferring `protected`, and the validator's "no
literal originates from a protected field" — each held a byte-identical compiled copy that
nothing held equal. Criterion 1 (no hostname, IP, URL scheme, `//`-path, CSS selector,
XPath or credential anywhere in the serialized artifact) is one error-level finding code,
`FORBIDDEN_CONTENT`, in `validate()`, run over every string leaf of the serialized tree with
the kind in `message` and the leaf path in `where`; `save`, `load` and the registry's
approval gate all inherit it from that one place, and `save` gates on that family only so a
draft carrying `RISK_UNCLASSIFIED` can still be persisted and registered. It had first been
built as a runtime check inside `save` alone, which the whole-phase review defeated with a
hand-edited file that `load` returned clean and the registry then approved by id. A test
scan pinned to a fixture is a second line, never the rule: it covers a field only if the
fixture populates it. `inputs`/`outputs` keys are constrained at the model to
`^[a-z][a-z0-9_]*$`, so the one surface a scan of values cannot see is closed at parse time.
Moving the scan into the validator exposed that `CapabilityPolicy.allowed_origins` — an
axis the plan invented when it expanded §4.4's seventh condition — could never pass it: an
origin is a hostname. The field is removed rather than exempted. §6.1 puts the allowlist in
configuration, never the artifact, and §4.2 decision 4 says an artifact carrying an origin is
tenant-locked by construction; a per-capability policy narrows paths and actions only, and a
capability that must be confined to a subset of a deployment's origins says so on the
deployment side. Criterion 1 therefore has exactly one deliberate exception, `inputs[].pattern`.
**Cost accepted:** `validate()` carries a text scan beside its structural checks, the
detectors have documented blind spots (IPv6 literals, two-label bare hosts, `//` mid-word)
and documented false positives (an id under a TLD-shaped suffix, `.NET`, `Foo::Bar`), and a
legitimate value that trips one is narrowed at the field — never met by softening the check.
Phase 7's compiler normalises parameter names the model proposes.

## D29 — The resolved overlay is the tenant variant, and the tool schema is a contract
`resolve(base, overlay)` sets `app.variant` to `overlay.targets`: a resolved base-plus-
overlay IS the tenant variant in §4.1's own vocabulary, and leaving it as `base` would have a
tenant-specific artifact claim to be the base once the store keys by `(id, version)`. The
lineage survives in the overlay's `base_id`/`base_version`. `export_tool_schema` formats and
does not validate — the three real gates (findings, `verified`, registry `status`) belong to
whoever serves the tool, and phase 9 owns enforcing them. It exports `sensitive` and
`redact` as inert extension keys: the calling agent sources the sensitive value and decides
what to do with it before our log writer is ever in the path, and a flag only our side can
see protects only our side. Its leak test asserts on keys and structure, not a stringified
blob — an input named `locator_hint` must not fail a guard that a `locator` key four levels
deep must.
**Cost accepted:** a consumer wanting the lineage reads the overlay, not the variant; a
mechanic smuggled into a value rather than a key would pass the structural leak test, which
is why it is paired with the top-level key set being exactly the contract's.

## D30 — The registry gate resolves the artifact itself and enforces §6.4
`write_registry_entry` resolves the artifact for `(id, version)` before it gates: a passed
artifact must match the key; otherwise the file is parsed from disk; identity is checked on
both paths, because a byte-copy of `v1.yaml` under another id had registered and approved
as that id. With nothing resolved, `approved` is refused (the store cannot approve what it
does not hold) and `draft` writes. A draft never depends on load-time validation passing —
draft is precisely the state for an artifact not yet fit for approval, and a fresh discovery
run's `RISK_UNCLASSIFIED` file must be registrable — while an approval always does.
`requires_human_approval` defaults to `True` and becomes un-clearable wherever the store
cannot prove it clearable: `False` is refused on any artifact carrying an `irreversible`
step, and refused when no artifact can be resolved at all. A silent rewrite to `True` would
hide the caller's disagreement; a flag that is always true carries no information, which is
why it is enforced rather than merely defaulted. `save` writes atomically through a sibling
temp file and `os.replace`, so a crash cannot leave a truncated file the immutability check
then refuses to overwrite forever; the registry is written through the same atomic helper
and re-validated through `RegistryEntry` on every write, so it can never hold a file
`read_registry` refuses.
Findings seen on either resolution path are logged, so approval-by-id is as observable as
`load`.
**Cost accepted:** an approved entry cannot be registered ahead of its file; the `artifact`
parameter is an optimisation for callers already holding it, never a way past the gate;
files land mode 0600 from the temp-file write, which phase 9's console — the first
cross-process reader — must widen consciously if it runs as another user.

## D31 — The result contract is closed, and a declared failure names its kind
`FailureKind` is the thirteen values §5.2 lists — not the eleven the contract card counted —
declared as contract vocabulary in `cua/artifact/models.py` beside `Outcome`, and imported by
`cua/replay/result.py` rather than redeclared (an identity test pins the one home). A `fail`
expect's `code` names the `FailureKind` it declares; `validate()` refuses a `fail` clause with a
missing or unknown code (`FAIL_CODE_NOT_A_FAILURE_KIND`) and a `business` clause with no code
(`BUSINESS_CODE_MISSING`), so `load` never hands the engine an artifact whose declared outcomes
it cannot express. The first draft collapsed a matched `fail` clause into `NO_BRANCH_MATCHED` —
the opposite of what that kind means — and asked a reader to disambiguate by message text; the
project had refused that shape three times already. `Mode` is `embedded | supervised`; only
`embedded` is implemented, and `supervised` raises rather than silently behaving like `embedded`.
`ReplayResult = Success | BusinessOutcome | Failure`, a plain union; `BusinessOutcome` is a
value, never an exception; `Failure.expected`/`observed` are non-empty at the model.
**Cost accepted:** an author wanting a bespoke failure name picks the nearest of thirteen;
phase 6 must replace the `supervised` raise with real behaviour rather than extend a stub.

## D32 — The settle loop is the only place replay waits, and it waits by polling a declared timeout
`settle(surface, step, settle_spec, recovery, *, clock)` polls under an injected `Clock`, so a
full eight-second declared timeout runs to completion in bounded virtual time under test. Per
poll: the pending native dialog first (§5.5 — dialogs never appear in the tree, so they are
matched by text against `Recovery.detect`), then recovery rules over the observation, then the
step's `expects` in declared order; the first matching clause wins even when its outcome is
absorbed (`retry`, or a node-text `dismiss`), because a skipped absorbed clause would be a
semantic no-op and declaring `retry` before `continue` is exactly the "spinner still up" guard
§5.1's ordering rule provides. A matched `retry` keeps polling against the same deadline — §5.3
rules a retry budget out. Every absorbed iteration, including the one that just dismissed a
dialog, passes through the deadline check and the poll sleep: the first draft looped
"immediately" after a dismiss and the reviewer measured 2.37 million dismisses in three seconds
with virtual time at zero. Empty `expects` returns `Continue` without polling. `ok=False` from
the surface with a dialog pending falls through to the loop; without one it is
`PRECONDITION_FAILED`. The surface gained `pending_dialog()` (the bare message) and
`locators.py` gained public `matches`/`name_matches`/`text_of` so the loop has one matching
rule, not a copy.
**Cost accepted:** one poll interval of latency after each dismissed dialog; a page-level
keystroke with no locator is unreplayable by construction.

## D33 — `replay()` returns exactly one of three shapes; nothing escapes
Every `SurfaceError` — from `resolve`, `act`, the loop's `observe`/`pending_dialog`, or the
failure helper's own capture — becomes `SESSION_LOST` (or a frame-less `Failure` of the
original kind); the first draft's `SESSION_LOST` path itself raised on a dead surface. A
`from_step` naming a step that bound nothing is `PRECONDITION_FAILED`; a `from_input` naming an
absent input is `INVALID_INPUT` before any browser exists; a declared output unbound when the
steps finish is `OUTPUT_VALIDATION_FAILED` (§5.4's "never a silently returned null" in its
second form — the first draft returned `Success {}`); a `navigate` with no target is
`PRECONDITION_FAILED`, never an empty URL. `from_step` resolves by the producing step's id — the
artifact's vocabulary in §4.1 and §4.4's graph — not by `into` name. `success.checkpoint` is
settled after the last step; unmatched, the replay is `NO_BRANCH_MATCHED` with `expected`
naming the checkpoint. The engine re-checks at its gate what `load` already refuses
(`risk=None`, bad expect codes) as `POLICY_BLOCKED`, for a caller that bypassed the store; and
every kind the engine can produce has a producer test. The rules the engine shares with the
validator — the failure-kind set, the expect-code check, the deny-first path check — live in
one place each (`FAILURE_KINDS`, `expect_code_problem`, `DeploymentAllowlist.permits_path`).
**Cost accepted:** two phase-3 files grew public names for phase 4's use; a raise out of the
engine is the one shape phase 6's session service cannot render, so none is permitted.

## D34 — Irreversible steps are gated precisely where §5.6 says
An artifact carrying an `irreversible` step is refused (`POLICY_BLOCKED`) without both
`confirm_irreversible=True` and an `idempotency_key`. The seen-set is keyed by
`(artifact.id, artifact.version, key)`, and the key is burned immediately before the
irreversible step's own `act` — nothing earlier can be a duplicate risk, so a replay that failed
on a prior locator may be retried under the same key while one that reached the act may not.
Tracking is in-memory and process-local: building durable retention now would duplicate phase
9's store or invent a second one, and §6.4 already names the human approval gate as the real
control. The allowlist check this phase ships is static — a `navigate` step's declared
`target.path` against the deployment's deny-then-allow prefixes, before the navigation is
attempted (proven against the mock app's mutating `/account/close` GET) — because live
enforcement of app-initiated redirects is Playwright-side machinery that is phase 5's.
**Cost accepted:** a restart forgets every prior invocation; a mid-session redirect to a denied
origin is not caught until phase 5.

## D35 — Failure evidence reaches disk, scrubbed, from structured events only
`cua/observability/` writes §10's layout — `evidence/<run_id>/{run.json, artifact.yaml,
result.json, trace.jsonl, screenshots/, snapshots/}` — and never attaches a `logging.Handler`:
every trace line is an explicit `event(...)` with structured fields, so the verbatim
`FORBIDDEN_CONTENT` text the store logs can never reach `/evidence/` by construction. A frame
is captured only for a failure that arose during or after a surface interaction; pre-act gate
failures carry an `evidence_ref` and no frame (§5.4: no session created), and a pending native
dialog skips the screenshot with an event saying why. Snapshots are re-scrubbed on write and
the written file is asserted clean — phase 2's half-discharged §3.7.2 property. `run.json` masks
inputs whose `InputSpec.sensitive` is true with `[REDACTED]`, the same marker and the same
accepted trade-off as the snapshot scrub. A sensitive input's value never enters a `Failure`:
the two sites that compose `expected`/`observed` from an input — the engine's `validate_inputs`
and the CLI's pair parser — write `[REDACTED]` in its place, so `result.json` and the CLI's
stdout need no masking pass that could not know what is sensitive anyway. The whole-phase review
reproduced the leak end to end (`"observed":"input 'member_id' was 'SECRET1'"`) in a seam no
task's fixtures covered: none paired a sensitive input with a failing validation. `run_id` is `run-YYYYMMDDHHMMSS-xxxx`, a shape pinned
by saving an artifact that carries it, so the criterion-1 scan can never refuse a run's own
provenance. `artifact.yaml` is written through `store.dump_yaml`, one dump for both files.
**Cost accepted:** a future event type embedding free text must scrub it explicitly — there is
no blanket net; evidence files are not written atomically, unlike the artifact store.

## D36 — The base URL is configuration, and the CLI validates before it launches
The base URL is a surface construction parameter — `launch_page(base_url)` opens the one page
with Playwright's `base_url` — never a `replay()` argument, so the engine passes `target.path`
and holds no host (§4.2 decision 4, D19). The CLI orders: `--mode` checked against `Mode`'s two
literals before any I/O (a bare string had let `--mode bogus` run unattended); `load` (a refusal
is one stderr line and exit 2, never a traceback); the evidence writer constructed and `run.json`
written — so a refused invocation still leaves a record under the `evidence_ref` it prints;
`--input` pairs parsed and integer-typed inputs coerced, with a malformed pair or a failed
coercion an `INVALID_INPUT` result; `validate_inputs` — the same public function the engine calls
first — strictly before a browser launches (§5.4 forbids a session for invalid input, not a
record of the refusal); `artifact.yaml` before the page opens; `result.json` after. `--evidence-root` defaults to `.` so the layout is `evidence/<run_id>/` at
the repository root, RULES.md's deliverable. A no-op Typer callback makes `replay` a subcommand
later phases attach siblings to. Phase 2's browser fixture became module-scoped: Playwright
allows one synchronous driver per thread, and the CLI test is the one place the real second
launch is exercised.
**Cost accepted:** one Chromium launch per test module that uses the fixture; login for the
mock app is the artifact's job, not the command's.

## D37 — Integration tests claim only what they can pin
Eight live tests drive the phase-1 application through every injected fault. Before any
locator was finalised, the member, expired and statement frames and the print-statement link's
accessible name were captured into the live-snapshot record — a fixture claim the cited capture
does not support is phase 3's M4 mistake in a new coat, and every provisional fixture then
matched. The slow-fault test claims end-to-end tolerance only: Playwright's `click` waits for an
initiated navigation by documented default, so no live test on the mock app's server-side delay
can prove the poll loop waited — that proof is the `FakeClock` suite's. The popup test pins
`NO_BRANCH_MATCHED` against a clause that is a real substring of the statement page, so
following the popup would flip the assertion; popups are not followed until a later phase adds
multi-window tracking to the surface.
**Cost accepted:** one integration path's timing is covered only by unit tests of the loop.

## D38 — The deployment allowlist has one type across the whole system
`PolicyConfig` (`cua/policy/config.py`) is `DeploymentAllowlist` extended, not a parallel
shape — the contract card's field names (`origins`, `allow_paths`) yield to the type phase 3
already declared and phase 4's `_narrowing_findings` already depends on. `PolicyConfig` adds
only `policy_mode`; the validator, the replay engine, and the live surface guard all consume
one `DeploymentAllowlist` (or its subclass), never a second copy that could drift from it
(D28). `DeploymentAllowlist` gains `permits_origin`, `permits_url` (origin leg, then
`permits_path` on the path with the query and fragment excluded), and `narrowed_by` (a
per-capability policy intersected with the deployment — a policy path or action survives
only if the deployment already permits it, so a policy cannot widen by construction, not only
by the validator's separate check). Both the queried path and every stored prefix are
normalised the same way (percent-decoded once, repeated slashes collapsed, `.`/`..`
segments resolved, query and fragment stripped): comparing only the query side left a
`navigate` target's own `..`-bearing query string able to walk a denied path's prefix
comparison out from under it, and comparing only the argument side left `narrowed_by`
non-idempotent, since a policy's own path spelling is stored verbatim as the next call's
prefix.
**Cost accepted:** a bare 13-17-digit account-shaped number is a different concern
(redaction, D42) and this normalisation says nothing about it; an operator's deny prefix
written with an unusual slash count or a stray `..` behaves as if it had been written
canonically, which is the safer reading, not a silent no-op.

## D39 — Enforcement on the live surface has two points, and each proves a different half
An application-initiated navigation is stopped two ways, deliberately, because one probe
showed neither alone is enough: `WebSurface` registers a context-level route on every
navigation request the page itself initiates (a link, a form, `goto`, a popup's first
request) and aborts a denied one before it reaches the server — proven against the mock
app's mutating `/account/close` GET by an unchanged ledger, not merely a refused result. A
server-side redirect is invisible to that interception (a POST that 303s is seen by the
route handler only as the POST; the redirect's own `framenavigated` event fires before the
initiating call even returns), so a `framenavigated` listener — attached to every page the
browser context opens, not only the one this surface drives, closing a popup-redirect blind
spot the first draft missed — detects a redirect that has already landed on a denied URL and
freezes the surface. `Surface.allowlist_violation()` is sticky (the first reason survives for
the life of the surface); once set, `act`/`act_on_index` raise rather than run, while
`observe`/`capture`/`pending_dialog` keep working so the violation can be evidenced. A guard
that raises is a denial naming the error (fail closed), never treated as permission.
**Cost accepted:** the redirect's own request has already reached the server by the time it
is caught — detection, not prevention, for that one case; a subresource fetch (script,
image, XHR) is not gated by either point; a listener sits on every popup for the life of the
surface even if none is ever opened.

## D40 — A recorded violation outranks every other reading a step could produce
Once `allowlist_violation()` is non-`None`, the replay engine reports `ALLOWLIST_VIOLATION`
ahead of whatever else the surface's call returned or raised — after every `act`, after
every `resolve` failure, after every settle (including a violation `settle()` itself detects
mid-poll, which now returns at once rather than waiting out the declared timeout against a
page that can never satisfy its `expects`), and after the checkpoint's own settle. The first
draft checked only after `act` and after settle when settle returned a matched outcome; a
`fill`/`select` step does not wait for an initiated navigation on this driver, so a
violation recorded during that step's own settle poll was previously masked as
`NO_BRANCH_MATCHED` or `PRECONDITION_FAILED` after a full timeout, and a violation recorded
during locator resolution was masked as `LOCATOR_NOT_FOUND` or its siblings. Each check site
consults the surface once and reports once — a violation `settle()` already translated is
not re-read and re-captured by the caller's own check immediately afterward.
**Cost accepted:** the check sites are enumerated by hand, not derived structurally from
"every place a `Surface` method is called" — a call site added later without the same
`_violation` guard reintroduces exactly this defect, and one such site (a `pending_dialog()`
check made after `act()` returns `ok=False`) is still open at this phase's close, flagged
for the human rather than fixed here (see the phase-5 ledger's final entry).

## D41 — Risk is heuristic first, human second, and the heuristic is asymmetric on purpose
`classify(action, accessible_name)` scores an action `safe`, `risky`, or `irreversible` from
the action type and the control's own accessible name — never a model, never a guess at
intent. `read`/`wait_for` can never rise above `safe` (observing cannot mutate); a `click`/
`press_key` on a name naming a commit verb (`post`, `delete`, `submit payment`, `close`
within two words of `account`) is `irreversible`; any action on a name matching the wider
`transfer|delete|close|post|submit payment` vocabulary is `risky` — including "Transfer
History", a read-only link, which is exactly the asymmetry §6.3 asks for: over-classifying
costs a human a moment at approval; a genuine transfer classified safe and run unattended
cannot be undone. An artifact author may declare a lower risk than the heuristic would —
`validate()` reports it as a warning, never an error, because the human at approval is the
final authority and must be able to downgrade deliberately — but never a higher one without
comment, since over-declaring costs nothing. The engine's own gate (`check_action`) then
refuses to run a `risky` or `irreversible` step unattended unless the artifact's registry
`status` is `approved`, and refuses any step with no risk classification at all, closing the
one path an artifact that bypassed `validate()` could otherwise take.
**Cost accepted:** the `close`-near-`account` phrase is bounded to two words apart, so "Close
this account" commits but a sentence that merely mentions an account two paragraphs later
does not — a deliberately narrower net than the unbounded first draft, at the cost of a
crafted three-word gap escaping the heaviest tier (it still lands on `risky`, not `safe`).

## D42 — Redaction has two vocabularies, and a shape is preserved only when shape carries meaning
A credential (a password, a token) is scrubbed to the literal `[REDACTED]` wherever the
existing rule already caught it (D15) — nothing about a credential's shape is worth keeping,
and phase 5 adds no third treatment for it. A PII-shaped value (an SSN, a card number, an
account number) is masked instead with its shape and its last four characters intact, so a
mismatch stays debuggable (§6.5) — the account shape is bounded at ten to twelve digits
specifically so the run-id timestamp's fourteen digits can never be caught by it, and a
shaped numeric JSON leaf (an `int`-typed output, an epoch-seconds trace field) is emitted as
the masked *string* rather than corrupting the surrounding JSON with an unquoted partial
mask. One `RedactingWriter` is the sole path evidence text reaches disk through — `RunLog`
and `EvidenceWriter` both go through it, and a test greps the observability package for a
direct file write to enforce that structurally rather than by convention. This resolves §10's
"result.json exactly as returned" in favour of §6.5's "masked in ... evidence": a `redact`-
flagged output is returned to the caller (stdout) unmasked and written to `result.json`
masked — the two channels are asserted apart by one test, not folded together.
**Cost accepted:** a value with no digits at all (a name, an address) tagged `redact` is
masked by turning every alphanumeric character into `*`, which destroys more than a digit
run's shape would; a short sensitive input's literal value, when it must be masked out of a
free-text reason rather than a declared field, is masked by substring replacement and can
therefore touch unrelated text that happens to contain it — the same accepted trade-off
`scrub_protected_values` already made, now a third site using it. The marker's own sign-off
remains owed to the repository owner, carried since phase 2.

## D43 — The CLI requires a deployment policy, and narrowing is checked where the browser is
`cua replay` refuses to run without `--policy PATH`: an unattended replay with no allowlist
is exactly the unbounded blast radius §6.7 warns against. The order is `--mode` literal,
then `load_policy` (a parse or schema refusal is one stderr line and exit 2, no evidence —
the same shape D36 gave a `load` refusal), then `load`, then `validate(artifact, policy)` —
the one place a per-capability policy's widening is checked against a real allowlist, since
the store's own `load` has none to check against — then the registry's recorded `status`,
then the writer, then inputs, then `launch_page`, then a `WebSurface` built with a guard
compiled from the same narrowed allowlist the engine receives. A syntactically malformed
policy file and a syntactically malformed artifact file are refused the same way — a parser
exception is caught and re-raised as the same `ValueError` every other malformed-input path
already produces, so an operator hand-editing either file sees one refusal shape, never a
traceback.
**Cost accepted:** a policy-load or widening refusal leaves no evidence directory at all
(D36's "a refused invocation still leaves a record" applies only from `load` onward, not to
a policy that never got that far); the deployment's own YAML file carries no schema
enforcement beyond what `PolicyConfig` itself validates.

**Standing note on this phase's whole-review close:** the final whole-phase review (five
findings, five folded minors) closed clean except one residual the scoped re-review
surfaced afterward — a fifth `SurfaceError`-to-`SESSION_LOST` translation site in the replay
engine (the `pending_dialog()` check made when an action reports `ok=False`) does not
consult the recorded-violation check D40 describes, and can therefore report `SESSION_LOST`
instead of `ALLOWLIST_VIOLATION` on a frozen surface. The security property is unaffected —
the surface stays frozen and no further action executes — only the diagnosis is wrong. Left
open deliberately rather than triggering a second fix wave; the phase-5 ledger names the
exact fix (mirror the two-line guard already applied at the other four sites) and this is
flagged to the repository owner at branch-finish for an explicit decision.

## D44 — The operator console bootstraps its cookie from the same shared token, in the clear
Task 7's `GET /console` accepts `CUA_OPERATOR_TOKEN` as a query-string parameter and, on a
match, sets an `httponly` cookie whose value is the raw token itself — the same secret the
JSON API's `Authorization: Bearer` header already checks. This was ruled in the phase-6 plan
(a plain `<a href>` navigation cannot send a bearer header, so a browser-reachable console
needs some other bootstrap) and confirmed by Task 7's own review as a real, independently
verified risk rather than an implementer defect: a query-string token lands in `uvicorn`'s
access logs and any reverse proxy in front of it, and `cua serve --host` is a free-form CLI
flag with no guard against binding a non-loopback interface (default is `127.0.0.1`). The
cookie carries the raw shared secret rather than an opaque per-session id, and `set_cookie`
does not pass `secure=True` (Starlette's default `samesite="lax"` already applies and
mitigates classic cross-site-POST CSRF, which is moot today since no console `POST` route
exists yet). No TLS exists anywhere in this codebase, so `secure=True` alone would not close
the underlying exposure and would break the cookie outright on a non-HTTPS non-loopback
bind — closing this needs a design decision (add TLS, or hard-guard loopback-only), not a
one-line fix.
**Cost accepted:** on the default loopback bind this is low-risk (same trust boundary as a
developer's own machine); if `cua serve --host` is ever pointed at a shared or non-loopback
interface, the operator token is exposed in plaintext logs and cookie storage with no
independent mitigation. Flagged to the repository owner, not fixed in Task 7 — the lower-risk
alternative (a `POST /console/login` form whose body isn't logged the same way as a query
string) is a future ruling, not this task's to make unilaterally.

## D45 — The console ships a claim/handback UI with no backing route
Task 7's `intervention.html` template renders claim and handback `<form action=...>` targets
that have no corresponding route in `cua/session/service.py` — only the two `GET` routes
(`/console`, `/console/interventions/{id}`) exist. Task 7's own review confirmed no task in
the 8-task phase-6 plan currently owns wiring these forms (Task 8 is integration tests only,
"no new production code"), and that inventing an operator-identity concept for the POST path
would be improvisation beyond a shared-token design that has no such concept today.
**Cost accepted:** the console is real, half-built UI an operator could open during a live
incident and find non-functional past the read-only view. Flagged to the repository owner
before the console is relied on for a real handoff — either a future task wires the forms
against the existing JSON API (reusing `_require_operator`'s bearer check via the cookie, or
prompting for the token per POST), or the forms are removed until that task exists.

## D46 — Resume re-verifies the escalating step itself, per §7.6; the shipped predecessor check was a bug
Task 8's reviewer confirmed a real conflict between spec §7.6 and this plan's own E6 ruling
that no test before Task 8 ever forced apart: §7.6 re-verifies "the `outcome: continue` clause
of the last completed step (falling back to `success.checkpoint` when the paused step is the
last one)"; E6 and the shipped `_resume_after` instead checked the escalating step's
**predecessor**, falling back only when the escalating step was the artifact's own **first**
step. Different entity, different trigger condition.
**Ruling: §7.6 is correct.** "The last completed step" is the escalating step itself — its own
`act()` already ran by the time a human hands back `Resolved`, and a human confirming
`Resolved` is confirming that step's own resulting state, which is exactly its own `continue`
clause. The stated fallback (checking `success.checkpoint` when that step is the artifact's
*last* one) only makes sense under this reading: a non-last step always declares a `continue`
clause because it needs one to advance past it, while a last step typically has none. Checking
the predecessor instead re-verifies a fact already true before the escalating step ever ran —
it can mask a genuinely unresumed middle step (replay proceeds to the next step's locator on
unverified state) and can spuriously re-escalate an otherwise-fine resume (when the
predecessor's own on-screen text has moved on by handback time even though
`success.checkpoint` already holds).
**Why not fixed silently:** this is production code in an already-reviewed, already-shipped
task (`cua/replay/engine.py`, part of Task 4's diff), so the fix is its own task (Task 9,
the `06-session.md` phase plan) with its own review, per this project's standing rule that a
ruling changing shipped code is never patched inside the session that made the ruling.
**Cost accepted:** the two existing `test_engine.py` cases for this path (both: the escalating
step is also the artifact's only or last step) pass unmodified under either reading, because
in both fixtures the predecessor's clause and `success.checkpoint` coincide by construction —
which is exactly why the discrepancy went undetected through Task 4's own review and Task 8's
close. Task 9 adds the discriminating test this gap needed.

## D47 — Phase 10 (assisted fallback) is cut, per D5's own pre-agreed order
The repository owner asked to wrap up the remaining project quickly. D5 named exactly this
contingency in advance: "Agreed cut order if time tightens: assisted fallback first, then
stability scoring, both documented in REPORT.md as designed but not built. The capability
catalog and cross-tenant reuse are not cut." Invoking it now is executing a decision already
made, not a new one.
**Decision:** the `10-assisted-fallback.md` phase plan is not implemented. Its design (already
written as a contract card, D5 tier 3: off by default, single step only, allowlist-checked,
never on risky actions, recorded as evidence, replay result marked assisted so it is never
reported as a clean deterministic replay) moves into REPORT.md §7 as designed-but-not-built,
citing that file directly rather than re-deriving the design in prose.
**What stays:** phases 7 (discovery loop), 8 (the real discovery run), 9 (catalog + approval
gate + stability score — D5's tier 1 and tier 2 stretch goals are not cut), and 11
(README/REPORT/evidence writeup) are all still required deliverables per `RULES.md` §3's
table and the plan's own gate column. "Quickly" is read as *build these four phases thin but
real* (RULES.md §5: "prefer a thin but real version of every required capability over a
polished subset"), not as cutting further into the core path — D5 already drew that line at
assisted fallback specifically so the four required phases would not be the next thing cut
if time ran short.
**Next candidate if more time pressure appears:** D5's own second-in-line cut is the
stability score specifically (not all of phase 9) — approval gating stays either way, since
`requires_human_approval` is load-bearing in the registry (D30) independent of stretch-goal
status.
**Cost accepted:** the brief's "at most one or two stretch goals" framing is already exceeded
by the three that remain (capability catalog, cross-tenant reuse, approval gating + stability
score) — D5 accepted that cost when it tiered four instead of picking one or two; this
decision does not revisit that, only executes the cut D5 already reserved for exactly this
moment.

## D48 — The discovery loop settles after every state-changing action
A click returns as soon as the click is dispatched, before the navigation it triggers has
committed, so an observation taken straight afterwards can still show the old page. The loop
therefore calls `settle_observation` (the replay engine's settle logic, for callers with no
`expects`) after each state-changing action and observes only once the page is quiet.
**Why:** observing early made the model act on a stale page and was the root of the flaky
end-to-end test (a race, not a flake).
**Cost accepted:** each such action waits for the settle poll, so a discovery run is slower.

## D49 — `compile()` prepends a navigate step to `app.entry`
A fresh browser sits on a blank page, so the CLI navigates to `app.entry` before discovery
starts and `compile()` writes the same `navigate` as the artifact's first step.
**Why:** the artifact must be replayable from a cold session, and a trace that begins on
"wherever the page happened to be" is not.
**Cost accepted:** the first step is synthesised rather than observed; a capability that
starts anywhere other than `app.entry` cannot be discovered yet.

## D50 — Bounded `LLMError` retry lives at the CLI boundary
`cua discover` wraps the Gemini client in `RetryingClient` (3 attempts, exponential backoff
from 1 s). The loop still counts an `LLMError` that survives the retries as one failed turn.
**Why:** live transient errors (404s, 5xx) are provider noise and should not spend the
loop's failure budget; the loop and its tests stay free of sleeping.
**Cost accepted:** permanent errors such as a bad key are retried too, at most about 9 calls
(roughly 9 s) before a clean stop.

## D51 — Self-verify can never approve, and reuses a different input value when given one
Self-verification replays the compiled artifact in a fresh session and may mark it `verified`,
never `approved` (D9). It replays with the discovery values unless `--verify-input name=value`
supplies a different one.
**Why:** a replay with the very value that was typed during discovery cannot tell a real
input binding from a baked-in literal.
**Cost accepted (acceptance criterion 8 limit):** without `--verify-input` the check reuses
the discovery value, so it proves repeatability, not parameterisation. Promotion is also plain
string equality, so a coincidental match can bind the wrong input.

## D52 — A secret found in the serialised artifact refuses the save
`cua discover` serialises the compiled artifact, and again after self-verify, and refuses to
save if any declared secret input value (`--secret-input`, or a `--verify-input` for one)
appears in it. Exit 1, one stderr line that does not echo the secret.
**Why:** `description=goal` and near-miss literals reach `artifacts/`, which is committed, and
evidence masking does not cover the store. `--goal` text is the operator's responsibility;
`build_messages` is not redesigned.
**Cost accepted:** the check is a literal match on the JSON text, so URL-encoded, split or
JSON-escaped forms (a secret containing a newline or tab) are not caught. The better fix, giving
the model input names and placeholders for sensitive values, is deferred.

## D53 — Discovered artifacts have no outputs until a later phase adds `--output`
`cua discover` passes `outputs={}` to `compile()`, so every model-proposed `output_name`
is downgraded to a local and a read-only capability (a balance lookup) returns nothing.
**Why:** the plan's Task 8 specifies it, and the spec's "model contributes the output schema"
(§8.3) needs a declared-output flag that was not built.
**Cost accepted (documented limit, review item I4):** discovery can find a lookup but cannot
yet return its value; a later phase adds `--output name[:type]` (repeatable).
