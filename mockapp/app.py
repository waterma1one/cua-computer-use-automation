from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from mockapp import faults
from mockapp.data import NOT_FOUND_MESSAGE, RESTRICTED_MESSAGE, resolve_member

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

BRANDS = {"base": "Meridian Credit Union", "b": "Lakeshore Federal"}

LOGIN_USER_ENV = "MOCKAPP_LOGIN_USER"
LOGIN_PASSWORD_ENV = "MOCKAPP_LOGIN_PASSWORD"
DEFAULT_LOGIN_USER = "teller"
DEFAULT_LOGIN_PASSWORD = "teller-demo-pw"

SESSION_COOKIE = "session"
SESSION_MAX_REQUESTS_ENV = "MOCKAPP_SESSION_MAX_REQUESTS"
DEFAULT_SESSION_MAX_REQUESTS = 20


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


def apply_fault(request: Request) -> HTMLResponse | None:
    """The shared fault hook. Call this first in any content route.

    Returns a `Response` the caller must return immediately, or `None` when the route
    should proceed with its normal logic. A new route (including one that does not
    exist yet) opts in with one line:

        if (resp := apply_fault(request)) is not None:
            return resp
    """
    fault = faults.resolve_fault(request)
    faults.maybe_delay(fault)
    if fault.error_500:
        raise HTTPException(status_code=500, detail="Injected server error")
    if fault.expired:
        return TEMPLATES.TemplateResponse(request, "expired.html", {})
    if fault.notice:
        return HTMLResponse(
            '<html><body><font size="4"><b>Scheduled maintenance</b></font>'
            '<p><font size="2">This system is temporarily unavailable for scheduled '
            "maintenance. Please try again shortly.</font></p></body></html>"
        )
    if fault.not_found:
        return TEMPLATES.TemplateResponse(
            request, "search.html", {"error": NOT_FOUND_MESSAGE}, status_code=404
        )
    if fault.denied:
        return TEMPLATES.TemplateResponse(
            request, "search.html", {"error": RESTRICTED_MESSAGE}, status_code=403
        )
    if fault.validation:
        return TEMPLATES.TemplateResponse(
            request, "search.html", {"error": "Validation error"}, status_code=422
        )
    if fault.dialog:
        return HTMLResponse(
            "<html><body><script>window.confirm('Unexpected dialog');</script></body></html>"
        )
    return None


def create_app(variant: str = "base") -> FastAPI:
    app = FastAPI(title=f"mockapp:{variant}")
    app.state.variant = variant
    app.state.sessions = {}
    brand = BRANDS[variant]

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
            return TEMPLATES.TemplateResponse(request, "expired.html", {})
        if status == "anonymous":
            return RedirectResponse("/login", status_code=303)
        return None

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "frameset.html", {"brand": brand})

    @app.get("/nav", response_class=HTMLResponse)
    def nav(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "nav.html", {"brand": brand})

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, error: str | None = None) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "login.html", {"error": error})

    @app.post("/login")
    def login(user: str = Form(...), password: str = Form(...)) -> RedirectResponse:
        expected_user = os.environ.get(LOGIN_USER_ENV, DEFAULT_LOGIN_USER)
        expected_password = os.environ.get(LOGIN_PASSWORD_ENV, DEFAULT_LOGIN_PASSWORD)
        if user != expected_user or password != expected_password:
            return RedirectResponse("/login?error=Invalid+username+or+password", status_code=303)
        max_requests = int(os.environ.get(SESSION_MAX_REQUESTS_ENV, DEFAULT_SESSION_MAX_REQUESTS))
        token = secrets.token_urlsafe(16)
        app.state.sessions[token] = Session(remaining=max_requests)
        response = RedirectResponse("/search", status_code=303)
        response.set_cookie(SESSION_COOKIE, token, httponly=True)
        return response

    @app.get("/search", response_class=HTMLResponse)
    def search_form(request: Request, error: str | None = None) -> HTMLResponse:
        if (resp := apply_fault(request)) is not None:
            return resp
        if (resp := require_login(request)) is not None:
            return resp
        return TEMPLATES.TemplateResponse(request, "search.html", {"error": error})

    @app.post("/search", response_model=None)
    def search(request: Request, mid: str = Form(...)) -> HTMLResponse | RedirectResponse:
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
        return RedirectResponse(f"/member/{mid}", status_code=303)

    @app.get("/member/{member_id}", response_class=HTMLResponse)
    def member_detail(request: Request, member_id: str) -> HTMLResponse:
        if (resp := apply_fault(request)) is not None:
            return resp
        if (resp := require_login(request)) is not None:
            return resp
        result = resolve_member(member_id)
        if result == NOT_FOUND_MESSAGE:
            return TEMPLATES.TemplateResponse(
                request, "search.html", {"error": NOT_FOUND_MESSAGE}, status_code=404
            )
        if result == RESTRICTED_MESSAGE:
            return TEMPLATES.TemplateResponse(
                request, "search.html", {"error": RESTRICTED_MESSAGE}, status_code=403
            )
        return TEMPLATES.TemplateResponse(request, "member.html", {"m": result})

    return app
