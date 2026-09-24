"""Spec §7: the session service. One `threading.Thread` per live session holds the entire
`replay()` call end to end, and is the ONLY thread that ever touches that session's `Page`
(E15) -- it opens the browser itself, builds the surface, runs the whole replay, and tears
down; every HTTP handler only ever touches shared, lock-protected in-memory state (the lease,
the interventions, a per-intervention pair of `threading.Event`s -- E16) to coordinate with
it, never a `Surface`/`Page` method directly. No asyncio anywhere (E11), the same
synchronous-only shape every earlier phase committed to. The one-thread rule is documented,
not enforced at runtime: every method below that touches `session.surface`,
`session.session_browser`, or a `Page` says which thread it runs on.

`SessionService` is what `cua.replay.engine.Escalator`'s structural contract is satisfied
by; `cua.replay.engine` never imports this module (E2, the same one-way coupling
`EvidenceSink` already proved out for `cua.observability`).

D40 in this module: every place a `SurfaceError` (or anything else raised on the session's
thread) is translated into a `Failure` goes through `_ending_failure`, which asks the surface
for a recorded allowlist violation first -- a violation the operator's own drive time tripped,
followed by an expiry or a park, is `ALLOWLIST_VIOLATION`, never `ESCALATION_TIMEOUT` or
`SESSION_LOST`. No HTTP handler calls a `Surface` method at all (E15), so no handler is a
translation site.
"""

from __future__ import annotations

import dataclasses
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict

from cua.artifact.models import Artifact, FailureKind, RegistryStatus
from cua.artifact.store import RegistryEntry, load, read_registry
from cua.artifact.validate import DeploymentAllowlist, validate
from cua.observability import EvidenceWriter
from cua.policy.allowlist import navigation_guard
from cua.policy.config import load_policy
from cua.replay.engine import replay as run_replay
from cua.replay.result import (
    CannotResolve,
    Failure,
    HandbackOutcome,
    ReplayResult,
    Resolved,
    ResolvedManually,
    RestartFrom,
)
from cua.replay.settle import SYSTEM_CLOCK
from cua.session.actions import HumanActionBracket
from cua.session.interventions import Intervention, InterventionExpired, Interventions
from cua.session.lease import Controller, Lease, LeasedSurface
from cua.surface.base import SurfaceError
from cua.surface.models import EvidenceFrame
from cua.surface.web import SessionBrowser, WebSurface, close_session_page, open_session_page

__all__ = ["SessionService", "create_app"]

# How long `POST /sessions` waits for the session's own thread to report its browser open.
# Playwright's own `chromium.launch()` gives up after 30s by default, raising on the session
# thread -- whose `finally` then sets `ready` anyway, so a failed launch answers 500 within
# that 30s, never by reaching this bound. The extra 15s covers the page open, the init
# script, and the surface construction that follow a slow launch. This bound is only ever
# reached by a launch that hangs without raising.
_SESSION_READY_TIMEOUT_S = 45.0

_REDACTION_MARKER = "[REDACTED]"

# Task 7's ruling: the browser console is a convenience layer over the same shared
# CUA_OPERATOR_TOKEN the JSON API's Authorization header already checks -- not a second
# auth mechanism. This cookie carries that same token value; `/interventions/*` never reads
# it (see `_require_operator`, unchanged).
_CONSOLE_COOKIE = "cua_operator"

_OUTCOME_NAMES: dict[type, str] = {
    Resolved: "resolved", ResolvedManually: "resolved_manually",
    RestartFrom: "restart_from", CannotResolve: "cannot_resolve",
}


def _escalation_index(artifact: Artifact, step_id: str | None) -> int:
    """The index of the step an intervention escalated on; `len(steps)` for the success
    checkpoint (`step_id=None`), which sits after every step."""
    if step_id is None:
        return len(artifact.steps)
    return next((i for i, s in enumerate(artifact.steps) if s.id == step_id), len(artifact.steps))


def _restart_range_is_safe(artifact: Artifact, target_index: int, escalation_index: int) -> bool:
    """E7/E13, computed once, for both callers: `restart_from` a step at `target_index` re-runs
    every step from the target through the escalating step T *inclusive* --
    `artifact.steps[target_index:escalation_index + 1]` -- and is allowed only when the
    target precedes T and every step in that range is `risk == "safe"`. T is in the range
    because the engine escalates an irreversible step's `Failure` even after its `act()` ran
    (trigger (c)), and a restart resumes the same run, whose act-site idempotency burn never
    refuses a second act: restarting before an irreversible T would perform T twice. (A
    deliberate re-run of a T that never acted is `Resolved`'s pre-act path, not this.) A
    checkpoint escalation (`escalation_index == len(steps)`) has no T to include.
    `_allowed_operator_actions` asks whether *any* target passes (to offer `restart_from` at
    all); the handback handler asks about the operator's requested target (to refuse it with
    a 400). One predicate, so the two never drift."""
    if not 0 <= target_index < escalation_index:
        return False
    return all(s.risk == "safe" for s in artifact.steps[target_index:escalation_index + 1])


def _restart_range_ids(artifact: Artifact, target_index: int, escalation_index: int) -> list[str]:
    return [s.id for s in artifact.steps[target_index:escalation_index + 1]]


@dataclass(frozen=True)
class _PrefixedSink:
    """Hands `HumanActionBracket` (whose frame names are fixed, `human_before`/`human_after`)
    a sink that prefixes each frame with its escalation's ordinal, so a second claim in the
    same session never overwrites the first claim's bracket evidence."""

    writer: EvidenceWriter
    prefix: str

    def event(self, **fields: object) -> None:
        self.writer.event(**fields)

    def frame(self, frame: EvidenceFrame, name: str) -> None:
        self.writer.frame(frame, f"{self.prefix}_{name}")


class _EscalationTimeout(Exception):
    """Internal: raised by `SessionService.escalate` when either phase of its two-phase wait
    (E16) times out with no outcome recorded, or a park request (E15/E16) arrived instead.
    Caught by `_run_session`, never allowed to propagate out of the thread uncaught (D33)."""


@dataclass
class _LiveSession:
    """Everything one live session needs, shared between its own dedicated thread and every
    HTTP-handler thread that touches it. Fields written only by the session's own thread
    (`surface`, `writer`, `session_browser`, `agent_token`, `ready`, `result`) are never
    written by an HTTP-handler thread; fields written by HTTP handlers (`lease`'s own state
    via `transfer`, the surface's token via `rebind`, `events`, `parked`) are always written
    under `SessionService._lock`.
    """

    session_id: str
    artifact: Artifact
    base_url: str
    root: Path
    deployment: DeploymentAllowlist
    status: RegistryStatus
    inputs: dict[str, Any]
    lease: Lease
    ttl_ms: int
    claim_ttl_ms: int
    logout_path: str | None
    confirm_irreversible: bool = False
    idempotency_key: str | None = None
    idempotency_claim: tuple[str, int, str] | None = None  # held in SessionService._live_keys
    ready: threading.Event = field(default_factory=threading.Event)
    surface: LeasedSurface | None = None          # set by _run_session, on its own thread
    writer: EvidenceWriter | None = None          # likewise
    session_browser: SessionBrowser | None = None  # likewise
    agent_token: str | None = None                # the token _run_session minted (E15)
    interventions: Interventions = field(default_factory=lambda: Interventions(clock=SYSTEM_CLOCK))
    # E12: exactly this one Interventions, constructed with exactly this one Clock, for the
    # whole life of the session -- no second Interventions/Clock is ever constructed for it.
    events: dict[str, tuple[threading.Event, threading.Event]] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)  # intervention ids, oldest first
    parked: bool = False
    result: ReplayResult | None = None  # published last, once teardown is complete


class _CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    version: int
    root: str
    base_url: str
    policy_path: str
    inputs: dict[str, Any]
    ttl_ms: int = 60_000
    claim_ttl_ms: int = 300_000
    logout_path: str | None = "/logout"
    # Passed straight through to `replay()`'s own E27 gate, which refuses an artifact with an
    # irreversible step unless both are given -- the service adds no approval of its own.
    confirm_irreversible: bool = False
    idempotency_key: str | None = None


class _ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operator_id: str


class _HandbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: str  # "resolved" | "resolved_manually" | "restart_from" | "cannot_resolve"
    step_id: str | None = None
    note: str | None = None


def _intervention_json(iv: Intervention) -> dict[str, Any]:
    body = dataclasses.asdict(iv)
    outcome = iv.handback_outcome
    body["handback_outcome"] = (
        None if outcome is None
        else {"outcome": _OUTCOME_NAMES[type(outcome)], **dataclasses.asdict(outcome)}
    )
    return body


class SessionService:
    def __init__(self, *, operator_token: str, headless: bool = True) -> None:
        self._operator_token = operator_token
        self._headless = headless
        self._sessions: dict[str, _LiveSession] = {}
        self._intervention_owner: dict[str, str] = {}  # intervention id -> session id
        # (artifact id, version, idempotency key) held by a session still running. The
        # engine's own D34 gate checks a key once, before any step, and burns it only at the
        # act -- unsynchronised, so two concurrent sessions with one key could both pass it.
        # The service is the first caller that runs replays concurrently, so it closes that
        # window here rather than in the shared engine.
        self._live_keys: set[tuple[str, int, str]] = set()
        self._lock = threading.Lock()
        self._local = threading.local()  # set once per session thread, read by escalate()

    def _current_session(self) -> _LiveSession:
        """The session this call is running on behalf of -- set by `_run_session` as the
        first thing it does on its own dedicated thread, read here by `escalate()`. A stray
        call to `escalate()` from any thread other than a session's own (a bug, never a
        normal path) raises `AttributeError` rather than silently reading another session's
        state -- `threading.local()` has no cross-thread visibility by construction, which is
        exactly the property this needs."""
        session: _LiveSession = self._local.session
        return session

    # --- Escalator protocol -- called ONLY from a session's own dedicated thread (E15) --------

    def escalate(
        self, *, step_id: str | None, kind: FailureKind, expected: str, observed: str,
        acted: bool,
    ) -> HandbackOutcome:
        """E16's two-phase wait: a claim phase bounded by `ttl_ms` from creation, then (once
        claimed) a resolve phase bounded by `claim_ttl_ms` from the claim -- mirroring Task
        2's `Interventions.claim`/`expire_if_due` model. Expiry is decided by that state
        machine, under the lock, not by the `Event.wait` timeout alone, so a claim that lands
        at the last moment and an expiry can never both win. Every `session.surface`/
        `session.writer` touch below runs on the session's own thread -- the one calling this
        method -- never crossing to an HTTP-handler thread (E15).

        I3: `acted` is the engine's own `Escalator.escalate` widening -- whether the
        escalating step's `act()` has already run. Recorded on the `Intervention` itself
        (`_allowed_operator_actions` decides what an operator may choose; this decides what
        they see about what choosing `resolved` actually does) so the console and
        `GET /sessions/{id}/interventions` can tell an operator, before they click
        "resolved", whether doing so re-runs an action that has not happened yet.
        """
        session = self._current_session()
        assert session.surface is not None and session.writer is not None
        surface, writer = session.surface, session.writer

        ordinal = f"escalation_{len(session.order) + 1}"
        frame_name = f"{ordinal}_{step_id or 'checkpoint'}"
        bracket_sink = _PrefixedSink(writer, ordinal)
        # C1: a pending native dialog blocks `capture()` (`page.screenshot()`) until
        # Playwright's own default timeout -- the same guard `cua.replay.engine._fail` and
        # `HumanActionBracket._capture` already apply. Without it, an escalation that fires
        # while a dialog is pending raises `SurfaceError` here, escapes `escalate()` and
        # `replay()`, and `_run_session`'s catch-all turns the whole run into `SESSION_LOST`
        # with no intervention ever created.
        try:
            message = surface.pending_dialog()
            if message is not None:
                writer.event(step_id=step_id, kind="capture_skipped",
                             reason="a native dialog is pending")
            else:
                writer.frame(surface.capture(), frame_name)  # at-escalation evidence
        except SurfaceError as exc:
            writer.event(step_id=step_id, kind="capture_failed", reason=str(exc))

        claim_event = threading.Event()
        handback_event = threading.Event()
        with self._lock:  # E19: create() and registering both Events is one atomic step
            parked = session.parked
            if not parked:
                iv = session.interventions.create(
                    run_id=session.session_id, goal=session.artifact.description,
                    capability_id=session.artifact.id, version=session.artifact.version,
                    step_id=step_id, reason_code=kind, expected=expected, observed=observed,
                    screenshot_ref=f"{writer.evidence_ref()}/screenshots/{frame_name}.png",
                    snapshot_ref=f"{writer.evidence_ref()}/snapshots/{frame_name}.yaml",
                    allowed_operator_actions=self._allowed_operator_actions(
                        session.artifact, step_id),
                    ttl_ms=session.ttl_ms, claim_ttl_ms=session.claim_ttl_ms, acted=acted,
                )
                session.events[iv.id] = (claim_event, handback_event)
                session.order.append(iv.id)
                self._intervention_owner[iv.id] = session.session_id
        if parked:  # a park request that arrived before this escalation opened
            self._park(session)
        # I4: a durable, sequenced record that a human intervention opened -- spec §7.7's
        # own phrase -- written on the session's own thread, the same thread that will
        # write every other event this escalation produces.
        writer.event(kind="intervention_created", intervention_id=iv.id, step_id=step_id,
                     reason_code=kind, acted=acted)

        if not self._wait_phase(session, iv, claim_event,
                                since_ms=iv.created_at, budget_ms=iv.ttl_ms):
            raise _EscalationTimeout(f"intervention {iv.id!r} was never claimed")

        with self._lock:
            claimed_by = iv.claimed_by
        # I4: spec §7.7's "operator identity is recorded on claim" -- durable, not just
        # the in-memory `claimed_by` `claim()` (an HTTP-handler thread) already sets.
        writer.event(kind="intervention_claimed", intervention_id=iv.id, operator_id=claimed_by)

        HumanActionBracket().before(surface, bracket_sink)  # first wake point (E16)

        assert iv.claimed_at is not None  # set by claim(), before claim_event was
        if not self._wait_phase(session, iv, handback_event,
                                since_ms=iv.claimed_at, budget_ms=iv.claim_ttl_ms):
            raise _EscalationTimeout(f"intervention {iv.id!r} was claimed but never handed back")

        with self._lock:
            parked = session.parked
        if parked:  # DELETE /sessions/{id}'s park path -- it signals handback_event itself
            self._park(session)

        HumanActionBracket().after(surface, bracket_sink)  # second wake point

        with self._lock:
            outcome = iv.handback_outcome
        if outcome is None:
            raise _EscalationTimeout(f"intervention {iv.id!r} handback recorded no outcome")
        # I4: the run log's own record that this intervention resolved, and how -- spec
        # §7.7's "interventions are sequenced in the run log".
        writer.event(kind="intervention_handed_back", intervention_id=iv.id,
                     outcome=_OUTCOME_NAMES[type(outcome)])
        return outcome

    def _wait_phase(
        self, session: _LiveSession, iv: Intervention, event: threading.Event, *,
        since_ms: int, budget_ms: int,
    ) -> bool:
        """Blocks until `event` is set (`True`) or the intervention expires (`False`). Runs on
        the session's own thread; touches no `Surface`."""
        while True:
            remaining_ms = since_ms + budget_ms - SYSTEM_CLOCK.monotonic_ms()
            if event.wait(timeout=max(remaining_ms, 0) / 1000):
                return True
            with self._lock:
                if event.is_set():
                    return True
                if session.interventions.expire_if_due(iv.id, clock=SYSTEM_CLOCK):
                    return False
            # Otherwise the Event's own timer ran a hair ahead of SYSTEM_CLOCK: wait out the
            # remainder rather than declaring an expiry the state machine does not agree with.

    def _park(self, session: _LiveSession) -> NoReturn:
        """E15/E16's park: capture on the session's own thread, then end the run. The lease
        is released only later, by `_run_session`'s `finally` on this same thread, so the
        capture is strictly ordered before the release."""
        assert session.surface is not None and session.writer is not None
        session.writer.frame(session.surface.capture(), "parked")
        raise _EscalationTimeout(f"session {session.session_id!r} was parked by an operator")

    def _allowed_operator_actions(self, artifact: Artifact, step_id: str | None) -> list[str]:
        actions = ["resolved", "resolved_manually", "cannot_resolve"]
        escalation = _escalation_index(artifact, step_id)
        if any(_restart_range_is_safe(artifact, i, escalation) for i in range(escalation)):
            actions.append("restart_from")
        return actions

    # --- the session's own thread -------------------------------------------------------

    def _run_session(self, session: _LiveSession) -> None:
        """Runs entirely on this session's own dedicated thread; every Playwright-touching
        call for this session happens here or in something this calls (`escalate()`,
        `replay()`), and nowhere else (E15)."""
        self._local.session = session
        result: ReplayResult
        try:
            session_browser = open_session_page(session.base_url, headless=self._headless)
            session.session_browser = session_browser
            session_browser.page.add_init_script(path=str(Path(__file__).parent / "capture.js"))
            web_surface = WebSurface(session_browser.page,
                                     navigation_guard=navigation_guard(session.deployment))
            with self._lock:
                token = session.lease.acquire("agent")
                parked = session.parked
            session.agent_token = token
            session.surface = LeasedSurface(web_surface, session.lease, token)
            session.writer = EvidenceWriter(session.root)
            # I4: the same run.json/artifact.yaml pair `cua replay` (`cua/cli.py`) writes,
            # in the same order, before the replay itself starts -- so a session that never
            # reaches `write_result` below (a crash before any step) still leaves an audit
            # trail naming what it was asked to do.
            session.writer.write_run(
                goal=session.artifact.description, capability=session.artifact.id,
                inputs=session.inputs, input_specs=session.artifact.inputs,
                policy_mode=session.artifact.provenance.policy_mode,
            )
            session.writer.write_artifact(session.artifact)
            session.ready.set()  # POST /sessions' own handler is waiting on this
            if parked:  # parked (or abandoned by a timed-out POST) before the replay began
                self._park(session)
            result = run_replay(
                session.artifact, session.inputs, session.surface, "supervised",
                deployment=session.deployment, status=session.status,
                confirm_irreversible=session.confirm_irreversible,
                idempotency_key=session.idempotency_key, escalator=self,
                evidence=session.writer, clock=SYSTEM_CLOCK,
            )
        except _EscalationTimeout as exc:
            result = self._ending_failure(
                session, "ESCALATION_TIMEOUT",
                "an intervention is claimed and handed back within its ttl", str(exc),
            )
        except Exception as exc:  # last-resort net (E11): a headed browser must never be left
                                   # parked forever because of an uncaught bug anywhere above
            if session.writer is not None:
                session.writer.event(kind="session_crashed", reason=str(exc))
            result = self._ending_failure(
                session, "SESSION_LOST", "the session completes normally",
                f"an unexpected error ended the session: {exc}",
            )
        finally:
            try:
                if session.session_browser is not None:
                    close_session_page(session.session_browser, logout_path=session.logout_path)
            finally:
                with self._lock:
                    session.lease.release()
                    if session.idempotency_claim is not None:
                        self._live_keys.discard(session.idempotency_claim)
                if session.writer is not None:
                    # I4: the same result.json `cua replay` writes, on the session's own
                    # thread -- `Success(assistance="human")` and every other outcome this
                    # phase can produce survive the process, not just this object's memory.
                    session.writer.write_result(
                        result, redacted_outputs={
                            name for name, spec in session.artifact.outputs.items()
                            if spec.redact
                        },
                    )
                session.result = result  # published last: a visible result means torn down
                session.ready.set()  # in case the launch itself failed before ready was set

    def _ending_failure(
        self, session: _LiveSession, kind: FailureKind, expected: str, observed: str,
    ) -> Failure:
        """The `Failure` for a run that ended by an exception on the session's thread. D40
        first: a recorded allowlist violation outranks the kind the caller would otherwise
        report, the same guard every `SurfaceError`-translating site in `cua.replay.engine`
        applies. A surface that cannot answer is treated as having no violation. Runs on the
        session's own thread (`allowlist_violation()` reads a recorded field, but it is still
        a `Surface` method, so it is kept where every other one is called)."""
        ref = (session.writer.evidence_ref() if session.writer is not None
               else f"evidence/{session.session_id}")
        reason: str | None = None
        if session.surface is not None:
            try:
                reason = session.surface.allowlist_violation()
            except SurfaceError:
                reason = None
        if reason is not None:
            reason = self._mask_sensitive_inputs(session, reason)
            if session.writer is not None:
                session.writer.event(kind="allowlist_violation", step_id=None, reason=reason)
            return Failure(
                kind="ALLOWLIST_VIOLATION", step_id=None,
                expected="every navigation the application initiates stays inside the "
                         "deployment allowlist",
                observed=reason, evidence_ref=ref,
            )
        return Failure(kind=kind, step_id=None, expected=expected, observed=observed,
                       evidence_ref=ref)

    @staticmethod
    def _mask_sensitive_inputs(session: _LiveSession, text: str) -> str:
        """A violation reason is built around a URL, whose query string can carry a value
        typed into a GET form -- the same masking `cua.replay.engine` applies to its own
        violation reasons (E31), for the declared `sensitive` inputs of this run."""
        for name, spec in session.artifact.inputs.items():
            if spec.sensitive and name in session.inputs:
                value = str(session.inputs[name])
                if value:
                    text = text.replace(value, _REDACTION_MARKER)
        return text

    # --- HTTP-handler side: shared state only, never a Surface/Page (E15) -----------------

    def _require_operator(self, authorization: str | None) -> None:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            token.encode(), self._operator_token.encode()
        ):
            raise HTTPException(status_code=401, detail="a valid operator bearer token is required")

    def _require_console(self, token: str | None, cookie: str | None) -> None:
        """`GET /console`'s own ruling (Task 7): a query-string `?token=` or the httponly
        cookie it sets, either checked against the same `CUA_OPERATOR_TOKEN`
        `_require_operator` checks for the JSON API -- a convenience layer over that one
        token, never a second mechanism. `_require_operator` itself is untouched."""
        for candidate in (token, cookie):
            if candidate is not None and secrets.compare_digest(
                candidate.encode(), self._operator_token.encode()
            ):
                return
        raise HTTPException(status_code=401, detail="a valid operator token is required")

    def list_all_interventions(self) -> list[Intervention]:
        """Every intervention across every live session, for `GET /console`'s listing."""
        with self._lock:
            return [iv for session in self._sessions.values()
                    for iv in session.interventions.list_all()]

    def console_intervention(self, iv_id: str) -> Intervention:
        """One intervention, for `GET /console/interventions/{id}`'s detail page."""
        with self._lock:
            _session, iv = self._owned_intervention(iv_id)
            return iv

    def _session(self, session_id: str) -> _LiveSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"no session {session_id!r}")
        return session

    def _owned_intervention(self, iv_id: str) -> tuple[_LiveSession, Intervention]:
        """Caller holds `self._lock`."""
        session_id = self._intervention_owner.get(iv_id)
        session = self._sessions.get(session_id) if session_id is not None else None
        iv = session.interventions.get(iv_id) if session is not None else None
        if session is None or iv is None:
            raise HTTPException(status_code=404, detail=f"no intervention {iv_id!r}")
        return session, iv

    def create_session(self, request: _CreateSessionRequest) -> dict[str, str]:
        # E12/D43: the same order `cua replay` established -- policy, then artifact, then
        # the narrowing check -- all before any thread or browser exists.
        root = Path(request.root)
        try:
            policy = load_policy(Path(request.policy_path))
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            artifact, _findings = load(request.artifact_id, request.version, root)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        errors = [f for f in validate(artifact, policy) if f.level == "error"]
        if errors:
            raise HTTPException(status_code=400,
                                detail="; ".join(f"{f.code}: {f.message}" for f in errors))
        status = (read_registry(root).get(artifact.id, {}).get(str(artifact.version),
                                                                RegistryEntry()).status)

        session = _LiveSession(
            session_id=f"sess-{secrets.token_hex(6)}", artifact=artifact,
            base_url=request.base_url, root=root,
            deployment=policy.narrowed_by(artifact.policy), status=status,
            inputs=request.inputs, lease=Lease(), ttl_ms=request.ttl_ms,
            claim_ttl_ms=request.claim_ttl_ms, logout_path=request.logout_path,
            confirm_irreversible=request.confirm_irreversible,
            idempotency_key=request.idempotency_key,
        )
        key = (None if request.idempotency_key is None
               else (artifact.id, artifact.version, request.idempotency_key))
        with self._lock:
            if key is not None:
                if key in self._live_keys:
                    raise HTTPException(
                        status_code=409,
                        detail=f"idempotency_key {request.idempotency_key!r} is held by a "
                               f"session of {artifact.id!r} v{artifact.version} still running")
                self._live_keys.add(key)
            session.idempotency_claim = key
            self._sessions[session.session_id] = session
        threading.Thread(target=self._run_session, args=(session,), daemon=True,
                         name=f"cua-{session.session_id}").start()

        if not session.ready.wait(timeout=_SESSION_READY_TIMEOUT_S):
            with self._lock:  # abandoned: the thread parks as soon as its launch completes
                session.parked = True
            raise HTTPException(status_code=500,
                                detail=f"session {session.session_id!r} did not open its browser")
        if session.agent_token is None:  # ready fired from the thread's `finally`: no browser
            raise HTTPException(status_code=500,
                                detail=f"session {session.session_id!r} failed to open its browser")
        return {"session_id": session.session_id, "lease_token": session.agent_token}

    def park(self, session_id: str) -> dict[str, Any]:
        """Signals the park; never touches the `Page` (E15). If the session is inside a
        claimed intervention's resolve phase, it wakes and parks at once; in a claim phase it
        parks the moment an operator claims (there is no human window to park before then);
        between escalations it parks at its next one."""
        session = self._session(session_id)
        with self._lock:
            session.parked = True
            if session.order:
                _claim_event, handback_event = session.events[session.order[-1]]
                handback_event.set()
        return {"session_id": session_id, "parked": True}

    def get_session(self, session_id: str) -> dict[str, Any]:
        session = self._session(session_id)
        result = session.result
        with self._lock:
            controller: Controller = session.lease.controller
        return {
            "session_id": session_id,
            "controller": controller,
            "result": result.model_dump(mode="json") if result is not None else None,
        }

    def list_interventions(self, session_id: str) -> list[dict[str, Any]]:
        session = self._session(session_id)
        with self._lock:  # E19: a consistent snapshot, never an intervention without Events
            return [_intervention_json(iv) for i in session.order
                    if (iv := session.interventions.get(i)) is not None]

    def claim(self, iv_id: str, request: _ClaimRequest) -> dict[str, Any]:
        with self._lock:
            session, iv = self._owned_intervention(iv_id)
            try:
                session.interventions.claim(iv_id, request.operator_id, clock=SYSTEM_CLOCK)
            except (InterventionExpired, ValueError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            # §7.4 "take control": the agent's LeasedSurface now holds a stale token, so the
            # automation cannot act while the operator does (E18).
            session.lease.transfer("operator")
            claim_event, _handback_event = session.events[iv_id]
            claim_event.set()
            return _intervention_json(iv)

    def handback(self, iv_id: str, request: _HandbackRequest) -> dict[str, Any]:
        with self._lock:
            session, iv = self._owned_intervention(iv_id)
            if iv.status != "claimed":
                raise HTTPException(status_code=409,
                                    detail=f"intervention {iv_id!r} is {iv.status!r}, not claimed")
            if session.lease.controller == "none":  # the thread ended (and released) mid-claim
                raise HTTPException(status_code=409,
                                    detail=f"session {session.session_id!r} has already ended")
            outcome = self._parse_outcome(session.artifact, iv, request)
            if isinstance(outcome, (Resolved, RestartFrom)):
                # Both re-enter replay()'s own loop on the session's thread (E6/E7), so the
                # agent must hold the lease again first -- on the SAME wrapper its thread is
                # already holding deep inside replay() (E18), never a reconstructed one.
                assert session.surface is not None  # set before any intervention can exist
                session.surface.rebind(session.lease.transfer("agent"))
            session.interventions.handback(iv_id, outcome)
            _claim_event, handback_event = session.events[iv_id]
            handback_event.set()
            return _intervention_json(iv)

    @staticmethod
    def _parse_outcome(
        artifact: Artifact, iv: Intervention, request: _HandbackRequest,
    ) -> HandbackOutcome:
        if request.outcome == "resolved":
            return Resolved()
        if request.outcome == "resolved_manually":
            return ResolvedManually()
        if request.outcome == "cannot_resolve":
            if not request.note:
                raise HTTPException(status_code=400, detail="cannot_resolve requires a note")
            return CannotResolve(note=request.note)
        if request.outcome == "restart_from":
            if request.step_id is None:
                raise HTTPException(status_code=400, detail="restart_from requires a step_id")
            target = next((i for i, s in enumerate(artifact.steps) if s.id == request.step_id),
                          None)
            if target is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"restart_from names {request.step_id!r}, which this capability "
                           f"does not declare ({[s.id for s in artifact.steps]})")
            escalation = _escalation_index(artifact, iv.step_id)
            if not _restart_range_is_safe(artifact, target, escalation):  # E7/E13
                raise HTTPException(
                    status_code=400,
                    detail=f"restart_from {request.step_id!r} would re-run steps "
                           f"{_restart_range_ids(artifact, target, escalation)}; every step "
                           f"re-run, the escalating step included, must be safe, and the "
                           f"target must precede the escalation")
            return RestartFrom(step_id=request.step_id)
        raise HTTPException(
            status_code=400,
            detail=f"outcome must be one of {list(_OUTCOME_NAMES.values())}; "
                   f"got {request.outcome!r}")


def create_app(*, operator_token: str, headless: bool = True) -> FastAPI:
    """The session service's HTTP surface. `headless=True` is every test's default; `cua
    serve` passes `False`, because a human handoff needs a browser the operator can see."""
    service = SessionService(operator_token=operator_token, headless=headless)
    app = FastAPI()
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "console"))

    @app.post("/sessions", status_code=201)
    def post_session(request: _CreateSessionRequest) -> dict[str, str]:
        return service.create_session(request)

    @app.delete("/sessions/{session_id}", status_code=202)
    def delete_session(session_id: str) -> dict[str, Any]:
        return service.park(session_id)

    @app.get("/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        return service.get_session(session_id)

    @app.get("/sessions/{session_id}/interventions")
    def get_interventions(session_id: str) -> list[dict[str, Any]]:
        return service.list_interventions(session_id)

    @app.post("/interventions/{iv_id}/claim")
    def post_claim(
        iv_id: str, request: _ClaimRequest, authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        service._require_operator(authorization)
        return service.claim(iv_id, request)

    @app.post("/interventions/{iv_id}/handback")
    def post_handback(
        iv_id: str, request: _HandbackRequest, authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        service._require_operator(authorization)
        return service.handback(iv_id, request)

    @app.get("/console")
    def get_console(request: Request, token: str | None = None) -> Response:
        service._require_console(token, request.cookies.get(_CONSOLE_COOKIE))
        response = templates.TemplateResponse(
            request, "index.html", {"interventions": service.list_all_interventions()},
        )
        if token is not None:
            response.set_cookie(_CONSOLE_COOKIE, service._operator_token, httponly=True)
        return response

    @app.get("/console/interventions/{iv_id}")
    def get_console_intervention(
        request: Request, iv_id: str, token: str | None = None,
    ) -> Response:
        service._require_console(token, request.cookies.get(_CONSOLE_COOKIE))
        iv = service.console_intervention(iv_id)
        response = templates.TemplateResponse(request, "intervention.html", {"iv": iv})
        if token is not None:
            response.set_cookie(_CONSOLE_COOKIE, service._operator_token, httponly=True)
        return response

    return app
