from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

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

    return app
