from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from mockapp.data import NOT_FOUND_MESSAGE, RESTRICTED_MESSAGE, resolve_member

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

BRANDS = {"base": "Meridian Credit Union", "b": "Lakeshore Federal"}


def create_app(variant: str = "base") -> FastAPI:
    app = FastAPI(title=f"mockapp:{variant}")
    app.state.variant = variant
    brand = BRANDS[variant]

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "frameset.html", {"brand": brand})

    @app.get("/nav", response_class=HTMLResponse)
    def nav(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "nav.html", {"brand": brand})

    @app.get("/search", response_class=HTMLResponse)
    def search_form(request: Request, error: str | None = None) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "search.html", {"error": error})

    @app.post("/search")
    def search(mid: str = Form(...)) -> RedirectResponse:
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
