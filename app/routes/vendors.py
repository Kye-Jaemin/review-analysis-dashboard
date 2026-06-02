"""Vendor analysis page + per-strength/weakness "why?" analysis.

Page route is server-rendered aggregation — pure read-only over data
already in the DB, no LLM. The per-item reason analysis (triggered by
clicking a strength or weakness on the page) is in this file too, since
it's just another view of the same vendor model.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.services.vendor_reasons import (
    delete_card,
    extract_reasons,
    find_card_lookup,
    get_card,
    list_saved_cards,
    reanalyze_card,
    save_card,
    toggle_card_hidden,
    update_card_label,
)
from app.services.vendors import list_vendors
from app.templating import render

router = APIRouter()


@router.get("/vendors")
async def vendors_page(request: Request, session: AsyncSession = Depends(get_session)):
    vendors = await list_vendors(session)
    # Build a (vendor_key, category_lower, band) → card_id map so the
    # template can render a 📌 marker next to strengths/weaknesses the
    # user already saved. Lookup happens off a composite index so even
    # a 1k-card workspace stays fast.
    saved_lookup = await find_card_lookup(session)
    return render(
        request,
        "vendors.html",
        vendors=vendors,
        saved_lookup=saved_lookup,
    )


# ----------------------------------------------------------------------------
# Reasons extraction (live LLM)
# ----------------------------------------------------------------------------


@router.get("/api/vendor-reasons")
async def vendor_reasons_endpoint(
    request: Request,
    vendor_key: str = Query(..., min_length=1, max_length=100),
    category_name: str = Query(..., min_length=1, max_length=200),
    band: str = Query(..., pattern="^(positive|negative)$"),
    summary_lang: str = Query("en"),
    model: Optional[str] = None,
    force: bool = Query(False),
    session: AsyncSession = Depends(get_session),
):
    """Live extraction. In-memory cached for 30 min by default; pass
    force=true to bypass."""
    try:
        return await extract_reasons(
            session,
            vendor_key=vendor_key,
            category_name=category_name,
            band=band,
            summary_lang=summary_lang,
            model=model,
            force=force,
        )
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


# ----------------------------------------------------------------------------
# Saved-card CRUD
# ----------------------------------------------------------------------------


class SaveReasonCardBody(BaseModel):
    label: Optional[str] = Field(None, max_length=200)
    # Caller posts the result blob it got from /api/vendor-reasons.
    result: dict


class PatchReasonCardBody(BaseModel):
    label: Optional[str] = Field(None, max_length=200)
    hidden: Optional[bool] = None


@router.get("/api/vendor-reason-cards")
async def list_reason_cards(
    include_hidden: int = 0,
    session: AsyncSession = Depends(get_session),
):
    return {
        "cards": await list_saved_cards(session, include_hidden=bool(include_hidden)),
    }


@router.get("/api/vendor-reason-cards/{card_id}")
async def get_reason_card(
    card_id: int, session: AsyncSession = Depends(get_session)
):
    card = await get_card(session, card_id)
    if not card:
        raise HTTPException(404, "card not found")
    return {
        "id": card.id,
        "vendor_key": card.vendor_key,
        "vendor_display": card.vendor_display,
        "category_name": card.category_name,
        "band": card.band,
        "label": card.label,
        "model_used": card.model_used,
        "sample_size": card.sample_size,
        "source_ids_snapshot": card.source_ids_snapshot,
        "reasons": card.reasons,
        "hidden": card.hidden,
        "created_at": card.created_at.isoformat() if card.created_at else None,
        "updated_at": card.updated_at.isoformat() if card.updated_at else None,
    }


@router.post("/api/vendor-reason-cards")
async def save_reason_card(
    body: SaveReasonCardBody,
    session: AsyncSession = Depends(get_session),
):
    try:
        card = await save_card(session, result=body.result, label=body.label)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "id": card.id,
        "vendor_key": card.vendor_key,
        "category_name": card.category_name,
        "band": card.band,
        "label": card.label,
    }


@router.post("/api/vendor-reason-cards/{card_id}/reanalyze")
async def reanalyze_reason_card(
    card_id: int,
    summary_lang: str = Query("en"),
    model: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    try:
        card = await reanalyze_card(
            session, card_id, summary_lang=summary_lang, model=model
        )
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    if not card:
        raise HTTPException(404, "card not found")
    return {
        "id": card.id,
        "vendor_key": card.vendor_key,
        "vendor_display": card.vendor_display,
        "category_name": card.category_name,
        "band": card.band,
        "label": card.label,
        "model_used": card.model_used,
        "sample_size": card.sample_size,
        "source_ids_snapshot": card.source_ids_snapshot,
        "reasons": card.reasons,
        "updated_at": card.updated_at.isoformat() if card.updated_at else None,
    }


@router.patch("/api/vendor-reason-cards/{card_id}")
async def patch_reason_card(
    card_id: int,
    body: PatchReasonCardBody,
    session: AsyncSession = Depends(get_session),
):
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
    return {"id": card.id, "label": card.label, "hidden": card.hidden}


@router.delete("/api/vendor-reason-cards/{card_id}")
async def delete_reason_card(
    card_id: int, session: AsyncSession = Depends(get_session)
):
    ok = await delete_card(session, card_id)
    if not ok:
        raise HTTPException(404, "card not found")
    return {"deleted": card_id}
