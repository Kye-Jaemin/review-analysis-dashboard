"""Competitive v3 routes — upload viewer + reason-click popup."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.services.competitive_v3 import (
    build_categorized_view,
    build_view,
    categorize_reasons_with_llm,
    has_top_category,
    parse_uploaded_file,
    reason_reviews,
)
from app.templating import render

router = APIRouter()


@router.get("/competitive-v3")
async def competitive_v3_page(request: Request):
    return render(request, "competitive_v3.html")


@router.post("/competitive-v3/parse")
async def competitive_v3_parse(
    request: Request,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
):
    """Multipart endpoint: read the uploaded CSV/Excel, enrich with
    vendor logos, return the HTML partial that renders the per-vendor
    reasons view. No LLM call — this is a pure viewer."""
    try:
        raw = await file.read()
        if not raw:
            raise ValueError("uploaded file is empty")
        rows = parse_uploaded_file(file.filename or "", raw)
    except Exception as e:  # noqa: BLE001
        return render(
            request,
            "_competitive_v3_results.html",
            error=f"파일 읽기 실패: {e}",
            result=None,
            csv_name=file.filename or "",
        )
    # v3's job is now to PRODUCE the categorization itself, not just
    # display a pre-categorized file. If the upload already carries a
    # 카테고리 column (e.g. a previously-categorized re-upload), we
    # honor it and skip the LLM step; otherwise we run Claude to
    # cluster the reasons into ~10 cross-vendor top categories before
    # building the view.
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
    return render(
        request,
        "_competitive_v3_categorized.html",
        result=result,
        csv_name=file.filename or "vendor_analysis",
        error=None,
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
