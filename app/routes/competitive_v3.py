"""Competitive v3 routes — LLM-categorize uploaded /vendors exports
into ~10 cross-vendor categories; save snapshots; XLSX export.
"""
from __future__ import annotations

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
    cards = await list_v3_cards(session, include_hidden=False)
    return render(request, "competitive_v3.html", saved_cards=cards)


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
    # Stash for the Save / Export buttons in the rendered partial.
    input_hash = hash_rows(rows)
    result["_input_hash"] = input_hash
    result["_model_used"] = settings.ANTHROPIC_MODEL
    remember_upload(input_hash, rows, result, file.filename or "")
    return render(
        request,
        "_competitive_v3_categorized.html",
        result=result,
        csv_name=file.filename or "vendor_analysis",
        error=None,
    )


@router.post("/competitive-v3/save")
async def competitive_v3_save(
    request: Request,
    input_hash: str = Form(...),
    label: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    """Persist the current upload's rows + result as a CompetitiveV3Card.
    Returns the saved-cards list partial so the UI can re-render the
    bookmarks panel."""
    upload = recall_upload(input_hash)
    if not upload:
        raise HTTPException(
            410,
            "이 분석 결과는 만료되었습니다. 파일을 다시 업로드해 주세요. "
            "(서버 메모리 캐시 TTL 30분)",
        )
    if not label.strip():
        raise HTTPException(422, "카드 이름이 비어있습니다.")
    card = await save_v3_card(
        session,
        label=label,
        rows=upload["rows"],
        result=upload["result"],
        model_used=settings.ANTHROPIC_MODEL,
        input_filename=upload.get("filename"),
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
    input_hash = hash_rows(rows)
    result["_input_hash"] = input_hash
    result["_file_name"] = card.input_filename or card.label
    result["_card_id"] = card.id
    result["_card_label"] = card.label
    result["_model_used"] = card.model_used
    remember_upload(input_hash, rows, result, card.input_filename or card.label)
    return render(
        request,
        "_competitive_v3_categorized.html",
        result=result,
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


@router.get("/competitive-v3/export.xlsx")
async def competitive_v3_export_xlsx(
    input_hash: Optional[str] = Query(None),
    card_id: Optional[int] = Query(None),
    session: AsyncSession = Depends(get_session),
):
    """Download the categorized result as a two-sheet xlsx.
    Source:
      - card_id → resolve from the saved CompetitiveV3Card row
      - input_hash → resolve from the short-lived upload cache
    """
    rows: Optional[list[dict]] = None
    result: Optional[dict] = None
    title_prefix = "competitive_v3"
    if card_id:
        card = await get_v3_card(session, card_id)
        if not card:
            raise HTTPException(404, "card not found")
        rows = list(card.input_rows or [])
        result = dict(card.result_payload or {})
        title_prefix = (card.label or title_prefix)[:60].replace(" ", "_")
    elif input_hash:
        upload = recall_upload(input_hash)
        if not upload:
            raise HTTPException(
                410, "분석 결과 캐시가 만료되었습니다. 다시 업로드해 주세요."
            )
        rows = upload["rows"]
        result = upload["result"]
    else:
        raise HTTPException(
            422, "input_hash 또는 card_id 중 하나가 필요합니다."
        )

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
