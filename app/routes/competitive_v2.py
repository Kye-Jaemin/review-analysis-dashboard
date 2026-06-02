"""Competitive analysis v2 — bottom-up success-factor clustering.

Mirrors the v1 endpoint shape (page + analyze + cards CRUD) but the
analyzer is bottom-up: no user factors, just CSV in → success-factor
categories out.
"""
from __future__ import annotations

import csv as _csv
import io
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.services.competitive_v2 import (
    analyze_csv_v2,
    delete_card,
    get_saved_card,
    list_saved_cards,
    reanalyze_card,
    save_card,
    toggle_card_hidden,
    update_card_label,
)
from app.templating import render

router = APIRouter()


class SaveV2CardBody(BaseModel):
    label: Optional[str] = Field(None, max_length=200)
    model_used: Optional[str] = Field(None, max_length=100)
    input_csv: list[dict]
    result_payload: dict


class PatchV2CardBody(BaseModel):
    label: Optional[str] = Field(None, max_length=200)
    hidden: Optional[bool] = None


# ----------------------------------------------------------------------------
# Page + analyze
# ----------------------------------------------------------------------------


@router.get("/competitive-v2")
async def competitive_v2_page(request: Request):
    return render(request, "competitive_v2.html")


def _parse_uploaded_csv(content: bytes) -> list[dict]:
    text = content.decode("utf-8-sig", errors="replace")
    reader = _csv.DictReader(io.StringIO(text))
    return [dict(r) for r in reader]


@router.post("/competitive-v2/analyze-csv")
async def competitive_v2_analyze_csv(
    request: Request,
    file: UploadFile = File(...),
    model: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_session),
):
    """Multipart endpoint: CSV → success-factor clustering."""
    try:
        raw = await file.read()
        if not raw:
            raise ValueError("uploaded file is empty")
        rows = _parse_uploaded_csv(raw)
    except Exception as e:  # noqa: BLE001
        return render(
            request,
            "_competitive_v2_results.html",
            error=f"CSV 읽기 실패: {e}",
            result=None,
            saved_card=None,
            csv_name=file.filename or "",
        )
    try:
        result = await analyze_csv_v2(rows=rows, model=model)
    except RuntimeError as e:
        return render(
            request,
            "_competitive_v2_results.html",
            error=f"LLM 분석 불가: {e}",
            result=None,
            saved_card=None,
            csv_name=file.filename or "",
        )
    except ValueError as e:
        return render(
            request,
            "_competitive_v2_results.html",
            error=str(e),
            result=None,
            saved_card=None,
            csv_name=file.filename or "",
        )

    result["_csv_name"] = file.filename or "vendor_analysis.csv"
    cleaned_rows = [dict(r) for r in (rows or []) if isinstance(r, dict)]
    return render(
        request,
        "_competitive_v2_results.html",
        result=result,
        input_csv=cleaned_rows,
        saved_card=None,
        csv_name=file.filename or "vendor_analysis.csv",
        error=None,
    )


# ----------------------------------------------------------------------------
# Saved cards
# ----------------------------------------------------------------------------


@router.get("/competitive-v2/cards")
async def competitive_v2_cards_partial(
    request: Request,
    include_hidden: int = 0,
    session: AsyncSession = Depends(get_session),
):
    cards = await list_saved_cards(session, include_hidden=bool(include_hidden))
    return render(
        request,
        "_competitive_v2_cards.html",
        cards=cards,
        include_hidden=bool(include_hidden),
    )


@router.post("/competitive-v2/cards")
async def competitive_v2_card_save(
    request: Request,
    body: SaveV2CardBody,
    session: AsyncSession = Depends(get_session),
):
    try:
        await save_card(
            session,
            label=body.label,
            input_csv=body.input_csv,
            result_payload=body.result_payload,
            model_used=body.model_used,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    cards = await list_saved_cards(session, include_hidden=False)
    return render(
        request,
        "_competitive_v2_cards.html",
        cards=cards,
        include_hidden=False,
    )


@router.get("/competitive-v2/cards/{card_id}/load")
async def competitive_v2_card_load(
    card_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    card = await get_saved_card(session, card_id)
    if not card:
        return render(
            request,
            "_competitive_v2_results.html",
            error=f"카드를 찾을 수 없습니다 (id={card_id})",
            result=None,
            saved_card=None,
            csv_name=None,
        )
    result = dict(card.result_payload or {})
    result["_csv_name"] = "(저장된 CSV)"
    return render(
        request,
        "_competitive_v2_results.html",
        result=result,
        input_csv=card.input_csv or [],
        saved_card={
            "id": card.id,
            "label": card.label,
            "model_used": card.model_used,
            "created_at": card.created_at.isoformat() if card.created_at else None,
            "updated_at": card.updated_at.isoformat() if card.updated_at else None,
        },
        csv_name=None,
        error=None,
    )


@router.post("/competitive-v2/cards/{card_id}/reanalyze")
async def competitive_v2_card_reanalyze(
    card_id: int,
    request: Request,
    model: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    try:
        card = await reanalyze_card(session, card_id, model=model)
    except RuntimeError as e:
        return render(
            request,
            "_competitive_v2_results.html",
            error=f"LLM 분석 불가: {e}",
            result=None,
            saved_card=None,
            csv_name=None,
        )
    if not card:
        return render(
            request,
            "_competitive_v2_results.html",
            error=f"카드를 찾을 수 없습니다 (id={card_id})",
            result=None,
            saved_card=None,
            csv_name=None,
        )
    result = dict(card.result_payload or {})
    result["_csv_name"] = "(저장된 CSV)"
    return render(
        request,
        "_competitive_v2_results.html",
        result=result,
        input_csv=card.input_csv or [],
        saved_card={
            "id": card.id,
            "label": card.label,
            "model_used": card.model_used,
            "created_at": card.created_at.isoformat() if card.created_at else None,
            "updated_at": card.updated_at.isoformat() if card.updated_at else None,
            "just_reanalyzed": True,
        },
        csv_name=None,
        error=None,
    )


@router.patch("/competitive-v2/cards/{card_id}")
async def competitive_v2_card_patch(
    card_id: int,
    body: PatchV2CardBody,
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


@router.delete("/competitive-v2/cards/{card_id}")
async def competitive_v2_card_delete(
    card_id: int,
    session: AsyncSession = Depends(get_session),
):
    ok = await delete_card(session, card_id)
    if not ok:
        raise HTTPException(404, "card not found")
    return {"deleted": card_id}
