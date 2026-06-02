"""Competitive-factor analysis page + API.

Three endpoints:

  GET  /competitive                     SSR page (input form, empty results)
  GET  /competitive/results?factor=...  HTML partial (HTMX swap target)
  GET  /api/competitive-rank?factor=... JSON (machine-readable)

The page never reloads — the form `hx-get`s the /results partial and
swaps it into the result panel. The JSON endpoint is exposed for
external use / debugging.

LLM cost is one Claude completion per submit; the result is computed
fresh each time (no cache in v1 — search is free-form so hit rate is
low). Sample reviews are fetched eagerly so expanding them in the UI
is a zero-RTT toggle.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.services.competitive import (
    DEFAULT_THRESHOLD,
    SAMPLE_LIMIT,
    rank_vendors_by_factor,
)
from app.templating import render

router = APIRouter()


@router.get("/competitive")
async def competitive_page(request: Request):
    """Landing page — input only, results loaded via HTMX into a partial."""
    return render(
        request,
        "competitive.html",
        default_threshold=DEFAULT_THRESHOLD,
    )


@router.get("/competitive/results")
async def competitive_results_partial(
    request: Request,
    factor: str = Query(..., min_length=1, max_length=200),
    threshold: float = Query(DEFAULT_THRESHOLD, ge=0.0, le=1.0),
    model: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    """HTML fragment for HTMX swap. Returns the results card directly.

    Errors render as an inline error block in the same swap target so
    the user sees what went wrong without a popup.
    """
    factor = (factor or "").strip()
    if not factor:
        return render(
            request,
            "_competitive_results.html",
            error="경쟁력 요소를 입력해주세요.",
            factor="",
            result=None,
        )
    try:
        result = await rank_vendors_by_factor(
            session,
            factor,
            model=model,
            threshold=threshold,
            sample_limit=SAMPLE_LIMIT,
        )
    except RuntimeError as e:
        # No API key on the deployment — same surface as a soft error.
        return render(
            request,
            "_competitive_results.html",
            error=f"LLM 분석 불가: {e}",
            factor=factor,
            result=None,
        )
    except ValueError as e:
        return render(
            request,
            "_competitive_results.html",
            error=str(e),
            factor=factor,
            result=None,
        )
    except Exception as e:  # noqa: BLE001 — surface any backend bug as-is
        return render(
            request,
            "_competitive_results.html",
            error=f"예기치 못한 오류: {type(e).__name__}: {e}",
            factor=factor,
            result=None,
        )
    return render(
        request,
        "_competitive_results.html",
        result=result,
        factor=factor,
        error=None,
    )


@router.get("/api/competitive-rank")
async def competitive_rank_api(
    factor: str = Query(..., min_length=1, max_length=200),
    threshold: float = Query(DEFAULT_THRESHOLD, ge=0.0, le=1.0),
    model: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    """JSON ranking — same payload the partial template iterates over."""
    factor = (factor or "").strip()
    if not factor:
        raise HTTPException(400, "factor is required")
    try:
        return await rank_vendors_by_factor(
            session,
            factor,
            model=model,
            threshold=threshold,
            sample_limit=SAMPLE_LIMIT,
        )
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
