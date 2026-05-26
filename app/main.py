from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.db import init_db
from app.i18n import COOKIE_MAX_AGE, COOKIE_NAME, SUPPORTED, detect_lang, make_t
from app.routes import analyze, categories, export, pages, reviews, sources
from app.templating import render, templates  # noqa: F401

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Review Dashboard", lifespan=lifespan)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def inject_lang(request: Request, call_next):
    request.state.lang = detect_lang(request)
    request.state.t = make_t(request.state.lang)
    response = await call_next(request)
    return response


app.state.render = render


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/set-language/{lang}")
async def set_language(lang: str, request: Request):
    if lang not in SUPPORTED:
        return JSONResponse({"error": "unsupported language"}, status_code=400)
    redirect_to = request.headers.get("referer") or "/"
    resp = RedirectResponse(url=redirect_to, status_code=303)
    resp.set_cookie(COOKIE_NAME, lang, max_age=COOKIE_MAX_AGE, httponly=False, samesite="lax")
    return resp


app.include_router(pages.router)
app.include_router(sources.router)
app.include_router(categories.router)
app.include_router(reviews.router)
app.include_router(analyze.router)
app.include_router(export.router)
