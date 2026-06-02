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


class FactorEntry(BaseModel):
    factor: str = Field(..., min_length=1, max_length=200)
    criteria: Optional[str] = Field(None, max_length=500)


class SaveCardBody(BaseModel):
    entries: list[FactorEntry] = Field(..., min_length=1, max_length=10)
    label: Optional[str] = Field(None, max_length=200)
    threshold: float = Field(DEFAULT_THRESHOLD, ge=0.0, le=1.0)
    model_used: Optional[str] = Field(None, max_length=100)
    input_csv: list[dict]
    result_payload: dict


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


def _parse_entries_form(raw) -> list[dict]:
    """Accept JSON-encoded list of {factor, criteria}, a list of plain
    strings (legacy), or a newline/comma-separated bare string. Returns
    a list of dicts {factor, criteria}.

    The page POSTs JSON; smoke tests / curl users may pass plain strings.
    """
    import json as _json
    parsed: list = []
    if isinstance(raw, list):
        parsed = list(raw)
    elif isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            parsed = []
        elif raw.startswith("["):
            try:
                tmp = _json.loads(raw)
                parsed = list(tmp) if isinstance(tmp, list) else []
            except Exception:
                parsed = [raw]
        else:
            # Newline / comma split — bare factor strings, no criteria.
            chunks: list[str] = []
            for line in raw.splitlines():
                chunks.extend(p for p in line.split(",") if p.strip())
            parsed = chunks or [raw]
    out: list[dict] = []
    for it in parsed:
        if isinstance(it, dict):
            factor = str(it.get("factor") or "").strip()
            criteria = str(it.get("criteria") or "").strip() or None
        else:
            factor = str(it or "").strip()
            criteria = None
        if not factor:
            continue
        out.append({"factor": factor, "criteria": criteria})
    return out


@router.post("/competitive/analyze-csv")
async def competitive_analyze_csv(
    request: Request,
    file: UploadFile = File(...),
    entries: str = Form(...),
    threshold: float = Form(DEFAULT_THRESHOLD),
    model: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_session),
):
    """Multipart endpoint: takes the CSV file + entries (JSON array of
    {factor, criteria}) + threshold, runs N parallel Claude calls and
    returns the HTML partial. Legacy clients can pass a bare-string
    `entries` field; _parse_entries_form coerces both."""
    entry_list = _parse_entries_form(entries)
    if not entry_list:
        return render(
            request,
            "_competitive_results.html",
            error="최소 1개 경쟁력 요소를 입력해주세요.",
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
            entries=entry_list,
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

    result["_csv_name"] = file.filename or "vendor_analysis.csv"
    cleaned_rows: list[dict] = []
    for r in rows or []:
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
        # Pydantic gave us a list of FactorEntry models; convert to
        # plain dicts for the service layer.
        entry_dicts = [
            {"factor": e.factor, "criteria": e.criteria}
            for e in body.entries
        ]
        await save_card(
            session,
            entries=entry_dicts,
            label=body.label,
            input_csv=body.input_csv,
            result_payload=body.result_payload,
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


def _build_load_result(card) -> dict:
    """Reconstitute the analyze_csv()-shaped dict from a saved card."""
    from app.services.competitive import _card_entries as _ce
    entries = _ce(card)
    factor_strings = [e["factor"] for e in entries]
    payload = card.result_rows
    if isinstance(payload, dict) and "groups" in payload:
        groups = payload.get("groups") or []
        total = int(payload.get("total_matched_rows") or sum(
            len(g.get("result_rows") or []) for g in groups
        ))
    elif isinstance(payload, list):
        groups = [{
            "factor": factor_strings[0] if factor_strings else card.factor,
            "criteria": entries[0]["criteria"] if entries else None,
            "result_rows": payload,
            "matched_count": len(payload),
        }]
        total = len(payload)
    else:
        groups = [
            {"factor": e["factor"], "criteria": e["criteria"],
             "result_rows": [], "matched_count": 0}
            for e in entries
        ]
        total = 0
    return {
        "entries": entries,
        "factors": factor_strings,
        "threshold": card.threshold,
        "input_row_count": len(card.input_csv or []),
        "strength_input_count": sum(
            1 for r in (card.input_csv or [])
            if (r.get("type") or "").strip().lower() == "strength"
        ),
        "groups": groups,
        "total_matched_rows": total,
        "model": card.model_used,
        "_csv_name": "(저장된 CSV)",
    }


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
    from app.services.competitive import _card_entries as _ce
    entries = _ce(card)
    return render(
        request,
        "_competitive_results.html",
        result=_build_load_result(card),
        input_csv=card.input_csv or [],
        saved_card={
            "id": card.id,
            "label": card.label,
            "factor": card.factor,
            "factors": [e["factor"] for e in entries],
            "entries": entries,
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
    from app.services.competitive import _card_entries as _ce
    entries = _ce(card)
    return render(
        request,
        "_competitive_results.html",
        result=_build_load_result(card),
        input_csv=card.input_csv or [],
        saved_card={
            "id": card.id,
            "label": card.label,
            "factor": card.factor,
            "factors": [e["factor"] for e in entries],
            "entries": entries,
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
