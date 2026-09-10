from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from mockapp import faults
from mockapp.data import (
    NOT_FOUND_MESSAGE,
    RESTRICTED_MESSAGE,
    Account,
    Member,
    resolve_account,
    resolve_member,
)

TEMPLATES_ROOT = Path(__file__).parent
BASE_TEMPLATE_DIR = TEMPLATES_ROOT / "templates"


def _variant_template_dirs(variant: str) -> list[Path]:
    """Search path for a variant's templates.

    A variant's own override directory (`templates_<variant>/`) is searched first,
    falling back to the base templates for anything it does not override -- Jinja2's
    `FileSystemLoader` tries each directory in the list in order and returns the first
    match it finds. The base variant has no override directory, so this resolves to
    exactly `[BASE_TEMPLATE_DIR]`, identical to the single fixed directory Task 1 bound
    `Jinja2Templates` to; that is what keeps variant A's rendered output byte-identical
    after this refactor.
    """
    if variant == "base":
        return [BASE_TEMPLATE_DIR]
    return [TEMPLATES_ROOT / f"templates_{variant}", BASE_TEMPLATE_DIR]


def _build_templates(variant: str) -> Jinja2Templates:
    return Jinja2Templates(directory=_variant_template_dirs(variant))


BRANDS = {"base": "Meridian Credit Union", "b": "Lakeshore Federal"}

LOGIN_USER_ENV = "MOCKAPP_LOGIN_USER"
LOGIN_PASSWORD_ENV = "MOCKAPP_LOGIN_PASSWORD"
DEFAULT_LOGIN_USER = "teller"
DEFAULT_LOGIN_PASSWORD = "teller-demo-pw"

SESSION_COOKIE = "session"
SESSION_MAX_REQUESTS_ENV = "MOCKAPP_SESSION_MAX_REQUESTS"
DEFAULT_SESSION_MAX_REQUESTS = 20

# A ledger of irreversible mutations (sub-account postings, account closures), module-level
# rather than on app.state on purpose: it must persist across separate create_app() calls so
# a test -- or a later phase's runner -- can prove that replaying a mutating request twice
# records it twice. See subaccount_post/account_close below for what gets appended.
LEDGER: list[dict[str, str]] = []


@dataclass
class Session:
    """A signed-in session, expired by request count rather than wall-clock time.

    Wall-clock expiry would force whatever forces it -- a test, or a live browser run
    driven by a separate process -- to either sleep in real time or reach into this
    process's clock, and the latter is not available to a live run at all. Counting
    requests instead lets both force expiry deterministically, in bounded real time,
    simply by making enough requests; `MOCKAPP_SESSION_MAX_REQUESTS` makes that bound
    small on demand.
    """

    remaining: int


DIALOG_FAULT_SCRIPT = "<script>window.confirm('Unexpected dialog');</script>"


def apply_fault(request: Request) -> HTMLResponse | None:
    """The shared fault hook. Call this first in any content route.

    Returns a `Response` the caller must return immediately, or `None` when the route
    should proceed with its normal logic. A new route (including one that does not
    exist yet) opts in with one line:

        if (resp := apply_fault(request)) is not None:
            return resp

    Every route also declares `resp: HTMLResponse | RedirectResponse | None` immediately
    before that line -- this function and `require_login` return different (if
    overlapping) unions, and without an explicit declaration mypy infers `resp`'s type
    from the first walrus assignment alone, then rejects the second (`require_login`'s)
    assignment as incompatible.

    The `dialog` fault is the one exception to "returns a Response to return
    immediately": a confirm() that replaced the whole page would never interrupt a real
    screen, which is not the condition a recovery rule has to handle. Instead it marks
    `request.state` and returns `None` so the route renders its normal page, and the
    `inject_dialog_fault` middleware registered in `create_app` splices the script into
    that page's body on the way out.
    """
    templates: Jinja2Templates = request.app.state.templates
    fault = faults.resolve_fault(request)
    faults.maybe_delay(fault)
    if fault.error_500:
        raise HTTPException(status_code=500, detail="Injected server error")
    if fault.expired:
        return templates.TemplateResponse(request, "expired.html", {})
    if fault.notice:
        return HTMLResponse(
            '<html><body><font size="4"><b>Scheduled maintenance</b></font>'
            '<p><font size="2">This system is temporarily unavailable for scheduled '
            "maintenance. Please try again shortly.</font></p></body></html>"
        )
    if fault.not_found:
        return templates.TemplateResponse(
            request, "search.html", {"error": NOT_FOUND_MESSAGE}, status_code=404
        )
    if fault.denied:
        return templates.TemplateResponse(
            request, "search.html", {"error": RESTRICTED_MESSAGE}, status_code=403
        )
    if fault.validation:
        return templates.TemplateResponse(
            request, "search.html", {"error": "Validation error"}, status_code=422
        )
    if fault.dialog:
        request.state.inject_dialog_fault = True
        return None
    return None


def resolve_or_error(request: Request, member_id: str) -> Member | HTMLResponse:
    """Resolve `member_id` to a `Member`, or the rendered not-found/restricted response.

    Every route that takes a member_id calls this (after `apply_fault`/`require_login`)
    instead of calling `resolve_member` and re-deriving the same "unknown record" /
    "restricted record" rendering itself. It wraps `resolve_member` -- the single place
    that decides visibility -- rather than replacing it; this only collapses what every
    caller did with the result. A caller checks the return with `isinstance(result,
    HTMLResponse)` and returns it immediately when true, otherwise treats `result` as the
    `Member`.
    """
    templates: Jinja2Templates = request.app.state.templates
    result = resolve_member(member_id)
    if isinstance(result, str):
        # isinstance, not `== NOT_FOUND_MESSAGE`, so mypy can narrow the union: an
        # equality check alone does not tell the type checker `result` is no longer a
        # possible Member on the fallthrough path below.
        status_code = 404 if result == NOT_FOUND_MESSAGE else 403
        return templates.TemplateResponse(
            request, "search.html", {"error": result}, status_code=status_code
        )
    return result


def resolve_account_or_error(
    request: Request, number: str
) -> tuple[Member, Account] | HTMLResponse:
    """Resolve `number` to its owning `(Member, Account)`, or the rendered not-found/
    restricted response. Mirrors `resolve_or_error` above -- same shape, same reason: it
    wraps `resolve_account` (which itself defers to `resolve_member`, the single place
    deciding visibility) rather than re-deriving the not-found/restricted rendering here.
    """
    templates: Jinja2Templates = request.app.state.templates
    result = resolve_account(number)
    if isinstance(result, str):
        status_code = 404 if result == NOT_FOUND_MESSAGE else 403
        return templates.TemplateResponse(
            request, "search.html", {"error": result}, status_code=status_code
        )
    return result


def create_app(variant: str = "base") -> FastAPI:
    app = FastAPI(title=f"mockapp:{variant}")
    app.state.variant = variant
    app.state.sessions = {}
    brand = BRANDS[variant]
    templates = _build_templates(variant)
    app.state.templates = templates

    @app.middleware("http")
    async def inject_dialog_fault(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if not getattr(request.state, "inject_dialog_fault", False):
            return response
        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("text/html"):
            return response
        # call_next's actual return value is Starlette's private _StreamingResponse,
        # which exposes body_iterator but is not part of Response's public, typed
        # interface -- hence the ignore rather than a cast onto an unexported type.
        body_iterator: AsyncIterator[bytes] = response.body_iterator  # type: ignore[attr-defined]
        body = b"".join([chunk async for chunk in body_iterator])
        if b"</body>" in body:
            body = body.replace(b"</body>", DIALOG_FAULT_SCRIPT.encode() + b"</body>", 1)
        else:
            body += DIALOG_FAULT_SCRIPT.encode()
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(
            content=body,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
        )

    def session_status(request: Request) -> str:
        """Returns anonymous / expired / authenticated. Spends one request of budget."""
        token = request.cookies.get(SESSION_COOKIE)
        session: Session | None = app.state.sessions.get(token) if token else None
        if session is None:
            return "anonymous"
        if session.remaining <= 0:
            del app.state.sessions[token]
            return "expired"
        session.remaining -= 1
        return "authenticated"

    def require_login(request: Request) -> HTMLResponse | RedirectResponse | None:
        status = session_status(request)
        if status == "expired":
            return templates.TemplateResponse(request, "expired.html", {})
        if status == "anonymous":
            return RedirectResponse("/login", status_code=303)
        return None

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "frameset.html", {"brand": brand})

    @app.get("/nav", response_class=HTMLResponse)
    def nav(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "nav.html", {"brand": brand})

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, error: str | None = None) -> HTMLResponse:
        return templates.TemplateResponse(request, "login.html", {"error": error})

    @app.post("/login")
    def login(user: str = Form(...), password: str = Form(...)) -> RedirectResponse:
        expected_user = os.environ.get(LOGIN_USER_ENV, DEFAULT_LOGIN_USER)
        expected_password = os.environ.get(LOGIN_PASSWORD_ENV, DEFAULT_LOGIN_PASSWORD)
        if user != expected_user or password != expected_password:
            return RedirectResponse("/login?error=Invalid+username+or+password", status_code=303)
        max_requests = int(os.environ.get(SESSION_MAX_REQUESTS_ENV) or DEFAULT_SESSION_MAX_REQUESTS)
        token = secrets.token_urlsafe(16)
        app.state.sessions[token] = Session(remaining=max_requests)
        response = RedirectResponse("/search", status_code=303)
        response.set_cookie(SESSION_COOKIE, token, httponly=True)
        return response

    @app.get("/search", response_model=None)
    def search_form(request: Request, error: str | None = None) -> HTMLResponse | RedirectResponse:
        resp: HTMLResponse | RedirectResponse | None  # see apply_fault's docstring
        if (resp := apply_fault(request)) is not None:
            return resp
        if (resp := require_login(request)) is not None:
            return resp
        return templates.TemplateResponse(request, "search.html", {"error": error})

    @app.post("/search", response_model=None)
    def search(request: Request, mid: str = Form(...)) -> HTMLResponse | RedirectResponse:
        resp: HTMLResponse | RedirectResponse | None  # see apply_fault's docstring
        if (resp := apply_fault(request)) is not None:
            return resp
        if (resp := require_login(request)) is not None:
            return resp
        if not (len(mid) == 5 and mid.isdigit()):
            return RedirectResponse("/search?error=Member+ID+must+be+five+digits", status_code=303)
        result = resolve_member(mid)
        if result == NOT_FOUND_MESSAGE:
            return RedirectResponse("/search?error=No+member+found", status_code=303)
        if result == RESTRICTED_MESSAGE:
            return RedirectResponse(
                "/search?error=You+are+not+authorized+to+view+this+record", status_code=303
            )
        # Variant B inserts an extra "Select branch" step between a successful search and
        # the member detail screen; the base variant goes straight to the member. This is
        # the one place that decision is made -- the /branch routes below exist for both
        # variants, but only variant B's search flow ever links to them.
        if variant == "b":
            return RedirectResponse(f"/branch?mid={mid}", status_code=303)
        return RedirectResponse(f"/member/{mid}", status_code=303)

    @app.get("/member/{member_id}", response_model=None)
    def member_detail(request: Request, member_id: str) -> HTMLResponse | RedirectResponse:
        resp: HTMLResponse | RedirectResponse | None  # see apply_fault's docstring
        if (resp := apply_fault(request)) is not None:
            return resp
        if (resp := require_login(request)) is not None:
            return resp
        result = resolve_or_error(request, member_id)
        if isinstance(result, HTMLResponse):
            return result
        return templates.TemplateResponse(request, "member.html", {"m": result})

    # /branch exists only to be variant B's inserted step (see POST /search above), so it
    # is registered only for variant B. Registering it unconditionally would leave a step
    # meant to distinguish B from A silently reachable in A's URL space -- unlinked from
    # A's UI, but visible to anything that enumerates registered routes rather than
    # observed navigation. Not registering it at all makes an unmatched request 404 the
    # ordinary way, rather than adding a variant check inside the handlers to fake one.
    if variant == "b":

        @app.get("/branch", response_model=None)
        def branch_select(request: Request, mid: str) -> HTMLResponse | RedirectResponse:
            resp: HTMLResponse | RedirectResponse | None
            if (resp := apply_fault(request)) is not None:
                return resp
            if (resp := require_login(request)) is not None:
                return resp
            result = resolve_or_error(request, mid)
            if isinstance(result, HTMLResponse):
                return result
            return templates.TemplateResponse(request, "branch.html", {"m": result})

        @app.post("/branch", response_model=None)
        def branch_selected(
            request: Request, mid: str = Form(...), branch: str = Form(...)
        ) -> HTMLResponse | RedirectResponse:
            resp: HTMLResponse | RedirectResponse | None
            if (resp := apply_fault(request)) is not None:
                return resp
            if (resp := require_login(request)) is not None:
                return resp
            result = resolve_or_error(request, mid)
            if isinstance(result, HTMLResponse):
                return result
            # `branch` is accepted (and validated as present, via Form(...)) but not
            # stored anywhere -- this step exists to be an inserted screen an automation
            # must expect and click through, not to model real branch-routing logic.
            return RedirectResponse(f"/member/{mid}", status_code=303)

    @app.get("/subaccount/new", response_model=None)
    def subaccount_new(request: Request, mid: str) -> HTMLResponse | RedirectResponse:
        resp: HTMLResponse | RedirectResponse | None  # see apply_fault's docstring
        if (resp := apply_fault(request)) is not None:
            return resp
        if (resp := require_login(request)) is not None:
            return resp
        result = resolve_or_error(request, mid)
        if isinstance(result, HTMLResponse):
            return result
        return templates.TemplateResponse(request, "subaccount_new.html", {"m": result})

    @app.post("/subaccount/review", response_model=None)
    def subaccount_review(
        request: Request, mid: str = Form(...), kind: str = Form(...)
    ) -> HTMLResponse | RedirectResponse:
        resp: HTMLResponse | RedirectResponse | None  # see apply_fault's docstring
        if (resp := apply_fault(request)) is not None:
            return resp
        if (resp := require_login(request)) is not None:
            return resp
        result = resolve_or_error(request, mid)
        if isinstance(result, HTMLResponse):
            return result
        return templates.TemplateResponse(
            request, "subaccount_confirm.html", {"mid": mid, "kind": kind}
        )

    @app.post("/subaccount/post", response_model=None)
    def subaccount_post(
        request: Request, mid: str = Form(...), kind: str = Form(...)
    ) -> HTMLResponse | RedirectResponse:
        resp: HTMLResponse | RedirectResponse | None  # see apply_fault's docstring
        if (resp := apply_fault(request)) is not None:
            return resp
        if (resp := require_login(request)) is not None:
            return resp
        result = resolve_or_error(request, mid)
        if isinstance(result, HTMLResponse):
            return result
        # The irreversible action itself. Deliberately NOT idempotent: every call appends a
        # new entry, even a byte-for-byte replay of the same mid/kind. A real teller system
        # would not dedupe a double-click on "Post" for free, and this mock must not either
        # -- a later phase's runner is responsible for refusing to blindly replay an action
        # classified this way, and that logic has nothing to prove itself against if this
        # route quietly protects itself.
        LEDGER.append({"action": "open_subaccount", "mid": mid, "kind": kind})
        return templates.TemplateResponse(
            request, "subaccount_done.html", {"mid": mid, "kind": kind}
        )

    @app.get("/statement/{member_id}", response_model=None)
    def statement(request: Request, member_id: str) -> HTMLResponse | RedirectResponse:
        resp: HTMLResponse | RedirectResponse | None  # see apply_fault's docstring
        if (resp := apply_fault(request)) is not None:
            return resp
        if (resp := require_login(request)) is not None:
            return resp
        result = resolve_or_error(request, member_id)
        if isinstance(result, HTMLResponse):
            return result
        return templates.TemplateResponse(request, "statement.html", {"m": result})

    @app.get("/account/close", response_model=None)
    def account_close(request: Request, number: str) -> HTMLResponse | RedirectResponse:
        resp: HTMLResponse | RedirectResponse | None  # see apply_fault's docstring
        if (resp := apply_fault(request)) is not None:
            return resp
        if (resp := require_login(request)) is not None:
            return resp
        # Deliberate hazard, not an oversight: this route mutates state (closes an account)
        # in response to a GET. Real legacy back offices have routes exactly like this,
        # written before "a GET must be safe" was taken seriously, and a computer-use
        # automation that assumes every GET is a safe read will walk straight through it.
        # A later phase's policy deny-rules are proven specifically against this route --
        # do not "fix" it by moving the mutation behind a POST.
        LEDGER.append({"action": "close_account", "number": number})
        return HTMLResponse(
            '<html><body><font size="4"><b>Account closed</b></font>'
            f'<p><font size="2">Account {number} has been closed.</font></p></body></html>'
        )

    # Registered after /account/close on purpose: Starlette matches path routes in
    # registration order, and "close" would otherwise be captured as this route's
    # {number} path parameter, shadowing the close hazard entirely.
    @app.get("/account/{number}", response_model=None)
    def account_detail(request: Request, number: str) -> HTMLResponse | RedirectResponse:
        resp: HTMLResponse | RedirectResponse | None  # see apply_fault's docstring
        if (resp := apply_fault(request)) is not None:
            return resp
        if (resp := require_login(request)) is not None:
            return resp
        result = resolve_account_or_error(request, number)
        if isinstance(result, HTMLResponse):
            return result
        member, account = result
        return templates.TemplateResponse(request, "account.html", {"m": member, "a": account})

    return app
