"""Competitive-factor analysis page + API.

NEW (post-0017): the analysis is now CSV-driven. The user uploads a
CSV exported from /vendors and the route classifies its strength rows
against a free-form competitive factor.

Endpoints:

  GET  /competitive                       SSR page (upload + form, empty results)
  POST /competitive/analyze-csv           HTML partial — takes (file, factor, threshold)
  GET  /competitive/cards                 sidebar partial (saved-card list)
  POST /competitive/cards                 save current analysis to DB
  GET  /competitive/cards/{id}/load       render saved card without LLM
  POST /competitive/cards/{id}/reanalyze  re-run LLM on saved CSV
  PATCH  /competitive/cards/{id}          rename / hide
  DELETE /competitive/cards/{id}          hard delete
"""
from __future__ import annotations

import csv as _csv
import io
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.services.competitive import (
    DEFAULT_THRESHOLD,
    analyze_csv,
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


class SaveCardBody(BaseModel):
    factor: str = Field(..., min_length=1, max_length=200)
    label: Optional[str] = Field(None, max_length=200)
    threshold: float = Field(DEFAULT_THRESHOLD, ge=0.0, le=1.0)
    model_used: Optional[str] = Field(None, max_length=100)
    input_csv: list[dict]
    result_rows: list[dict]


class PatchCardBody(BaseModel):
    label: Optional[str] = Field(None, max_length=200)
    hidden: Optional[bool] = None


# ----------------------------------------------------------------------------
# Page + CSV analysis
# ----------------------------------------------------------------------------


@router.get("/competitive")
async def competitive_page(request: Request):
    """Landing page — upload + input form. Results swap into a partial via HTMX."""
    return render(
        request,
        "competitive.html",
        default_threshold=DEFAULT_THRESHOLD,
    )


def _parse_uploaded_csv(content: bytes) -> list[dict]:
    """Parse the /vendors export CSV (UTF-8 with optional BOM)."""
    text = content.decode("utf-8-sig", errors="replace")
    reader = _csv.DictReader(io.StringIO(text))
    rows: list[dict] = []
    for r in reader:
        # csv.DictReader gives plain dicts with string values; the
        # service-layer _normalize_csv_row coerces types so leave
        # everything as-string here.
        rows.append(dict(r))
    return rows


@router.post("/competitive/analyze-csv")
async def competitive_analyze_csv(
    request: Request,
    file: UploadFile = File(...),
    factor: str = Form(...),
    threshold: float = Form(DEFAULT_THRESHOLD),
    model: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_session),
):
    """Multipart endpoint: takes the CSV file + factor + threshold, runs
    one Claude call to score strength categories, returns the HTML
    partial with the result table."""
    factor = (factor or "").strip()
    if not factor:
        return render(
            request,
            "_competitive_results.html",
            error="경쟁력 요소를 입력해주세요.",
            result=None,
            saved_card=None,
            csv_name=None,
        )
    try:
        raw = await file.read()
        if not raw:
            raise ValueError("uploaded file is empty")
        rows = _parse_uploaded_csv(raw)
    except Exception as e:  # noqa: BLE001
        return render(
            request,
            "_competitive_results.html",
            error=f"CSV 읽기 실패: {e}",
            result=None,
            saved_card=None,
            csv_name=file.filename or "",
        )

    try:
        result = await analyze_csv(
            factor=factor,
            rows=rows,
            threshold=threshold,
            model=model,
        )
    except RuntimeError as e:
        return render(
            request,
            "_competitive_results.html",
            error=f"LLM 분석 불가: {e}",
            result=None,
            saved_card=None,
            csv_name=file.filename or "",
        )
    except ValueError as e:
        return render(
            request,
            "_competitive_results.html",
            error=str(e),
            result=None,
            saved_card=None,
            csv_name=file.filename or "",
        )

    # Attach the CSV name so the partial can show "vendor_analysis.csv (50행)"
    # AND embed the parsed input rows in a hidden script tag so the save
    # button can POST them back without re-uploading.
    result["_csv_name"] = file.filename or "vendor_analysis.csv"
    # Stash the cleaned rows the LLM actually saw (post-normalization)
    # so saving from the page records exactly what was analyzed.
    cleaned_rows: list[dict] = []
    for r in rows or []:
        # Replicate service-layer normalization shape (we just stash
        # what we got — service will re-normalize on reanalyze).
        if isinstance(r, dict):
            cleaned_rows.append({k: v for k, v in r.items()})
    return render(
        request,
        "_competitive_results.html",
        result=result,
        input_csv=cleaned_rows,
        saved_card=None,
        csv_name=file.filename or "vendor_analysis.csv",
        error=None,
    )


# ----------------------------------------------------------------------------
# Saved cards
# ----------------------------------------------------------------------------


@router.get("/competitive/cards")
async def competitive_cards_partial(
    request: Request,
    include_hidden: int = 0,
    session: AsyncSession = Depends(get_session),
):
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
    try:
        await save_card(
            session,
            factor=body.factor,
            label=body.label,
            input_csv=body.input_csv,
            result_rows=body.result_rows,
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
    card = await get_saved_card(session, card_id)
    if not card:
        return render(
            request,
            "_competitive_results.html",
            error=f"카드를 찾을 수 없습니다 (id={card_id})",
            result=None,
            saved_card=None,
            csv_name=None,
        )
    result = {
        "factor": card.factor,
        "threshold": card.threshold,
        "input_row_count": len(card.input_csv or []),
        "strength_input_count": sum(
            1 for r in (card.input_csv or [])
            if (r.get("type") or "").strip().lower() == "strength"
        ),
        "result_rows": card.result_rows or [],
        "model": card.model_used,
        "_csv_name": "(저장된 CSV)",
    }
    return render(
        request,
        "_competitive_results.html",
        result=result,
        input_csv=card.input_csv or [],
        saved_card={
            "id": card.id,
            "label": card.label,
            "factor": card.factor,
            "threshold": card.threshold,
            "model_used": card.model_used,
            "created_at": card.created_at.isoformat() if card.created_at else None,
            "updated_at": card.updated_at.isoformat() if card.updated_at else None,
        },
        csv_name=None,
        error=None,
    )


@router.post("/competitive/cards/{card_id}/reanalyze")
async def competitive_card_reanalyze(
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
            "_competitive_results.html",
            error=f"LLM 분석 불가: {e}",
            result=None,
            saved_card=None,
            csv_name=None,
        )
    if not card:
        return render(
            request,
            "_competitive_results.html",
            error=f"카드를 찾을 수 없습니다 (id={card_id})",
            result=None,
            saved_card=None,
            csv_name=None,
        )
    result = {
        "factor": card.factor,
        "threshold": card.threshold,
        "input_row_count": len(card.input_csv or []),
        "strength_input_count": sum(
            1 for r in (card.input_csv or [])
            if (r.get("type") or "").strip().lower() == "strength"
        ),
        "result_rows": card.result_rows or [],
        "model": card.model_used,
        "_csv_name": "(저장된 CSV)",
    }
    return render(
        request,
        "_competitive_results.html",
        result=result,
        input_csv=card.input_csv or [],
        saved_card={
            "id": card.id,
            "label": card.label,
            "factor": card.factor,
            "threshold": card.threshold,
            "model_used": card.model_used,
            "created_at": card.created_at.isoformat() if card.created_at else None,
            "updated_at": card.updated_at.isoformat() if card.updated_at else None,
            "just_reanalyzed": True,
        },
        csv_name=None,
        error=None,
    )


@router.patch("/competitive/cards/{card_id}")
async def competitive_card_patch(
    card_id: int,
    body: PatchCardBody,
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
