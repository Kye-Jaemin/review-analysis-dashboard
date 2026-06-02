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

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.services.competitive import (
    DEFAULT_THRESHOLD,
    SAMPLE_LIMIT,
    compute_drift,
    delete_card,
    get_saved_card,
    list_saved_cards,
    rank_vendors_by_factor,
    reanalyze_card,
    reorder_cards,
    save_card,
    toggle_card_hidden,
    update_card_label,
)
from app.templating import render

router = APIRouter()


class SaveCardBody(BaseModel):
    factor: str = Field(..., min_length=1, max_length=200)
    label: Optional[str] = Field(None, max_length=200)
    threshold: float = Field(DEFAULT_THRESHOLD, ge=0.0, le=1.0)
    model_used: Optional[str] = Field(None, max_length=100)
    result: dict


class PatchCardBody(BaseModel):
    label: Optional[str] = Field(None, max_length=200)
    hidden: Optional[bool] = None


class ReorderBody(BaseModel):
    ids: list[int] = Field(default_factory=list)


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
        saved_card=None,
        drift=None,
    )


# ----------------------------------------------------------------------------
# Saved-card endpoints
# ----------------------------------------------------------------------------


@router.get("/competitive/cards")
async def competitive_cards_partial(
    request: Request,
    include_hidden: int = 0,
    session: AsyncSession = Depends(get_session),
):
    """Sidebar partial — card list (HTMX target). Metadata only."""
    cards = await list_saved_cards(session, include_hidden=bool(include_hidden))
    return render(
        request,
        "_competitive_cards.html",
        cards=cards,
        include_hidden=bool(include_hidden),
    )


@router.post("/competitive/cards")
async def competitive_card_save(
    request: Request,
    body: SaveCardBody,
    session: AsyncSession = Depends(get_session),
):
    """Persist the current analysis. HTMX-friendly: returns the new card
    list partial so the sidebar refreshes in one round-trip."""
    try:
        await save_card(
            session,
            factor=body.factor,
            label=body.label,
            result=body.result,
            threshold=body.threshold,
            model_used=body.model_used,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    cards = await list_saved_cards(session, include_hidden=False)
    return render(
        request,
        "_competitive_cards.html",
        cards=cards,
        include_hidden=False,
    )


@router.get("/competitive/cards/{card_id}/load")
async def competitive_card_load(
    card_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Load a saved card → render the results partial from the cached
    JSON. No LLM call. Same partial template as fresh analyses, with
    `saved_card` populated so the meta bar shows the saved-state header
    instead of the save button."""
    card = await get_saved_card(session, card_id)
    if not card:
        return render(
            request,
            "_competitive_results.html",
            error=f"카드를 찾을 수 없습니다 (id={card_id})",
            factor="",
            result=None,
            saved_card=None,
            drift=None,
        )
    drift = await compute_drift(session, card)
    return render(
        request,
        "_competitive_results.html",
        result=card.result,
        factor=card.factor,
        error=None,
        saved_card={
            "id": card.id,
            "label": card.label,
            "factor": card.factor,
            "threshold": card.threshold,
            "model_used": card.model_used,
            "created_at": card.created_at.isoformat() if card.created_at else None,
            "updated_at": card.updated_at.isoformat() if card.updated_at else None,
        },
        drift=drift,
    )


@router.post("/competitive/cards/{card_id}/reanalyze")
async def competitive_card_reanalyze(
    card_id: int,
    request: Request,
    model: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    """Re-run rank_vendors_by_factor() with the card's saved factor,
    overwrite this row's result, and return the updated results partial.
    No access gate — single Claude call is light."""
    try:
        card = await reanalyze_card(session, card_id, model=model)
    except RuntimeError as e:
        return render(
            request,
            "_competitive_results.html",
            error=f"LLM 분석 불가: {e}",
            factor="",
            result=None,
            saved_card=None,
            drift=None,
        )
    if not card:
        return render(
            request,
            "_competitive_results.html",
            error=f"카드를 찾을 수 없습니다 (id={card_id})",
            factor="",
            result=None,
            saved_card=None,
            drift=None,
        )
    drift = await compute_drift(session, card)
    return render(
        request,
        "_competitive_results.html",
        result=card.result,
        factor=card.factor,
        error=None,
        saved_card={
            "id": card.id,
            "label": card.label,
            "factor": card.factor,
            "threshold": card.threshold,
            "model_used": card.model_used,
            "created_at": card.created_at.isoformat() if card.created_at else None,
            "updated_at": card.updated_at.isoformat() if card.updated_at else None,
            # Re-analyze marker so the UI can flash a "방금 재분석됨" badge
            "just_reanalyzed": True,
        },
        drift=drift,
    )


@router.patch("/competitive/cards/{card_id}")
async def competitive_card_patch(
    card_id: int,
    body: PatchCardBody,
    session: AsyncSession = Depends(get_session),
):
    """Rename or toggle hidden. JSON response, not HTML — the UI either
    refreshes the sidebar via a follow-up call or rebuilds inline."""
    card = None
    if body.label is not None:
        try:
            card = await update_card_label(session, card_id, body.label)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not card:
            raise HTTPException(404, "card not found")
    if body.hidden is not None:
        card = await toggle_card_hidden(session, card_id, body.hidden)
        if not card:
            raise HTTPException(404, "card not found")
    if card is None:
        raise HTTPException(400, "nothing to update")
    return {
        "id": card.id,
        "label": card.label,
        "hidden": card.hidden,
    }


@router.delete("/competitive/cards/{card_id}")
async def competitive_card_delete(
    card_id: int,
    session: AsyncSession = Depends(get_session),
):
    ok = await delete_card(session, card_id)
    if not ok:
        raise HTTPException(404, "card not found")
    return {"deleted": card_id}


@router.post("/competitive/cards/reorder")
async def competitive_cards_reorder(
    body: ReorderBody,
    session: AsyncSession = Depends(get_session),
):
    n = await reorder_cards(session, body.ids)
    return {"updated": n}


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
