"""Shared Jinja2Templates instance to avoid circular imports."""
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.i18n import SUPPORTED, make_t

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def render(request: Request, template: str, **ctx):
    lang = getattr(request.state, "lang", "en")
    t = getattr(request.state, "t", make_t(lang))
    ctx.setdefault("lang", lang)
    ctx.setdefault("t", t)
    ctx.setdefault("settings", settings)
    ctx.setdefault("supported_langs", SUPPORTED)
    return templates.TemplateResponse(request, template, ctx)
