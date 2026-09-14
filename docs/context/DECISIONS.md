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
