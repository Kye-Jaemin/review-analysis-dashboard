"""Competitive v3 routes — LLM-categorize uploaded /vendors exports
into ~10 cross-vendor categories; save snapshots; XLSX export.
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.services.competitive_v3 import (
    build_categorized_view,
    categorize_reasons_with_llm,
    delete_v3_card,
    export_categorized_xlsx,
    get_v3_card,
    has_top_category,
    hash_rows,
    list_v3_cards,
    parse_uploaded_file,
    reason_reviews,
    recall_upload,
    remember_upload,
    save_v3_card,
    toggle_v3_card_hidden,
    update_v3_card_label,
)
from app.templating import render

router = APIRouter()


def _export_filename(prefix: str = "competitive_v3") -> str:
    """Stamp the export filename with the current local timestamp so
    repeated downloads don't overwrite each other. ASCII-only so the
    Content-Disposition header stays in the simple latin-1 path."""
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}.xlsx"


@router.get("/competitive-v3")
async def competitive_v3_page(
    request: Request, session: AsyncSession = Depends(get_session)
):
    # Defensive: if the saved-cards table doesn't exist yet (e.g. the
    # 0020 migration hasn't run on this DB), the page still renders —
    # the saved-cards panel just shows empty + a diagnostic message so
    # we can see WHY it's empty (vs. silently swallowing errors that
    # mask broken DB state).
    cards: list[dict] = []
    cards_error: Optional[str] = None
    try:
        cards = await list_v3_cards(session, include_hidden=False)
    except Exception as e:  # noqa: BLE001
        cards_error = f"{type(e).__name__}: {e}"
    return render(
        request, "competitive_v3.html",
        saved_cards=cards,
        cards_error=cards_error,
    )


@router.post("/competitive-v3/parse")
async def competitive_v3_parse(
    request: Request,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
):
    """Read the uploaded CSV/Excel; if the file lacks a 카테고리 column,
    run Claude to cluster every reason into ~10 cross-vendor top
    categories; build the categorized view; remember the rows + result
    in the short-lived upload cache so Save / Export buttons in the
    returned partial can recover them without re-uploading."""
    try:
        raw = await file.read()
        if not raw:
            raise ValueError("uploaded file is empty")
        rows = parse_uploaded_file(file.filename or "", raw)
    except Exception as e:  # noqa: BLE001
        return render(
            request,
            "_competitive_v3_categorized.html",
            error=f"파일 읽기 실패: {e}",
            result=None,
            csv_name=file.filename or "",
        )
    lang = getattr(request.state, "lang", "ko")
    try:
        if not has_top_category(rows):
            rows = await categorize_reasons_with_llm(rows, lang=lang)
        result = await build_categorized_view(session, rows)
    except Exception as e:  # noqa: BLE001
        return render(
            request,
            "_competitive_v3_categorized.html",
            error=f"파일 처리 실패: {e}",
            result=None,
            csv_name=file.filename or "",
        )
    result["_file_name"] = file.filename or "vendor_analysis"
    result["_input_hash"] = hash_rows(rows)
    result["_model_used"] = settings.ANTHROPIC_MODEL
    # Best-effort in-memory cache for back-compat callers that still
    # pass input_hash; the canonical Save / Export path is now embedded
    # JSON in the partial (see raw_rows below) so it survives worker
    # restarts and free-tier process recycling.
    remember_upload(result["_input_hash"], rows, result, file.filename or "")
    return render(
        request,
        "_competitive_v3_categorized.html",
        result=result,
        raw_rows=rows,
        csv_name=file.filename or "vendor_analysis",
        error=None,
    )


@router.post("/competitive-v3/save")
async def competitive_v3_save(
    request: Request,
    label: str = Form(...),
    rows_json: str = Form(...),
    result_json: str = Form(...),
    filename: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    """Persist a v3 analysis as a CompetitiveV3Card.

    Takes rows + result as JSON form fields embedded directly in the
    result partial (no server-side cache lookup) so Save works after
    worker restarts, Render free-tier process recycling, or just
    after long browsing time. Returns the saved-cards list partial
    so the UI can re-render the bookmarks panel inline.
    """
    if not label.strip():
        raise HTTPException(422, "카드 이름이 비어있습니다.")
    try:
        rows = json.loads(rows_json)
        result = json.loads(result_json)
    except json.JSONDecodeError as e:
        raise HTTPException(422, f"invalid payload: {e}")
    if not isinstance(rows, list) or not isinstance(result, dict):
        raise HTTPException(422, "payload shape mismatch")
    card = await save_v3_card(
        session,
        label=label,
        rows=rows,
        result=result,
        model_used=settings.ANTHROPIC_MODEL,
        input_filename=(filename or "").strip() or None,
    )
    cards = await list_v3_cards(session, include_hidden=False)
    return render(
        request,
        "_competitive_v3_saved_cards.html",
        saved_cards=cards,
        just_saved_id=card.id,
    )


@router.get("/competitive-v3/cards/{card_id}/load")
async def competitive_v3_card_load(
    card_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Hydrate a saved card back into the rendered categorized view
    partial. Re-stashes the rows in the upload cache so the per-card
    Export button works the same way as for a freshly-uploaded file."""
    card = await get_v3_card(session, card_id)
    if not card:
        raise HTTPException(404, "card not found")
    result = dict(card.result_payload or {})
    rows = list(card.input_rows or [])
    result["_input_hash"] = hash_rows(rows)
    result["_file_name"] = card.input_filename or card.label
    result["_card_id"] = card.id
    result["_card_label"] = card.label
    result["_model_used"] = card.model_used
    remember_upload(result["_input_hash"], rows, result, card.input_filename or card.label)
    return render(
        request,
        "_competitive_v3_categorized.html",
        result=result,
        raw_rows=rows,
        csv_name=card.input_filename or card.label,
        error=None,
    )


@router.post("/competitive-v3/cards/{card_id}/rename")
async def competitive_v3_card_rename(
    card_id: int,
    request: Request,
    label: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    card = await update_v3_card_label(session, card_id, label)
    if not card:
        raise HTTPException(404, "card not found")
    cards = await list_v3_cards(session, include_hidden=False)
    return render(
        request, "_competitive_v3_saved_cards.html", saved_cards=cards
    )


@router.post("/competitive-v3/cards/{card_id}/hide")
async def competitive_v3_card_hide(
    card_id: int,
    request: Request,
    hidden: int = Form(1),
    session: AsyncSession = Depends(get_session),
):
    card = await toggle_v3_card_hidden(session, card_id, bool(int(hidden)))
    if not card:
        raise HTTPException(404, "card not found")
    cards = await list_v3_cards(session, include_hidden=False)
    return render(
        request, "_competitive_v3_saved_cards.html", saved_cards=cards
    )


@router.post("/competitive-v3/cards/{card_id}/delete")
async def competitive_v3_card_delete(
    card_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    ok = await delete_v3_card(session, card_id)
    if not ok:
        raise HTTPException(404, "card not found")
    cards = await list_v3_cards(session, include_hidden=False)
    return render(
        request, "_competitive_v3_saved_cards.html", saved_cards=cards
    )


def _xlsx_response(rows: list, result: dict, title_prefix: str) -> Response:
    body = export_categorized_xlsx(rows or [], result or {})
    return Response(
        content=body,
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": (
                f'attachment; filename="{_export_filename(title_prefix)}"'
            )
        },
    )


@router.get("/competitive-v3/export.xlsx")
async def competitive_v3_export_xlsx_get(
    card_id: Optional[int] = Query(None),
    input_hash: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
):
    """GET path — saved cards (card_id) export here. Kept for direct
    links from the saved-cards list. The input_hash path is left as a
    best-effort fallback for the legacy memory cache."""
    if card_id:
        card = await get_v3_card(session, card_id)
        if not card:
            raise HTTPException(404, "card not found")
        rows = list(card.input_rows or [])
        result = dict(card.result_payload or {})
        title_prefix = (card.label or "competitive_v3")[:60].replace(" ", "_")
        return _xlsx_response(rows, result, title_prefix)
    if input_hash:
        upload = recall_upload(input_hash)
        if upload:
            return _xlsx_response(upload["rows"], upload["result"], "competitive_v3")
        raise HTTPException(
            410,
            "분석 결과 캐시가 만료되었습니다. 페이지 상단의 📥 엑셀 내보내기 "
            "버튼을 다시 누르거나, 먼저 카드로 저장한 뒤 카드에서 내보내세요.",
        )
    raise HTTPException(422, "card_id 또는 input_hash 중 하나가 필요합니다.")


@router.post("/competitive-v3/export.xlsx")
async def competitive_v3_export_xlsx_post(
    rows_json: str = Form(...),
    result_json: str = Form(...),
):
    """POST path — fresh-upload XLSX export. The current upload's rows
    + result are submitted directly from the rendered partial so the
    download works even after the server's in-memory upload cache has
    expired (Render free-tier process recycling)."""
    try:
        rows = json.loads(rows_json)
        result = json.loads(result_json)
    except json.JSONDecodeError as e:
        raise HTTPException(422, f"invalid payload: {e}")
    return _xlsx_response(rows, result, "competitive_v3")


@router.get("/competitive-v3/reason-reviews")
async def competitive_v3_reason_reviews(
    vendor_key: str = Query(..., min_length=1, max_length=100),
    category_name: str = Query(..., min_length=1, max_length=200),
    band: str = Query("positive", pattern="^(positive|negative)$"),
    reason_text: str = Query(..., min_length=1, max_length=300),
    session: AsyncSession = Depends(get_session),
):
    """Look up the saved VendorReasonCard for (vendor_key, category,
    band), find the reason with the given text, and return the
    hydrated reviews from its review_ids. Read-only — no LLM call."""
    try:
        return await reason_reviews(
            session,
            vendor_key=vendor_key,
            category_name=category_name,
            band=band,
            reason_text=reason_text,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e))
