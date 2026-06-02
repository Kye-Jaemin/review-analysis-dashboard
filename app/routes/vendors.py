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


def _unwrap_reasons(stored):
    """Tolerate both shapes: old cards have `reasons` = bare list, new
    cards have `reasons` = {reasons: [...], simple_responses: {...}}.
    Returns (reasons_list, simple_responses_dict)."""
    if isinstance(stored, list):
        return stored, {"count": 0, "examples": []}
    if isinstance(stored, dict):
        return (
            stored.get("reasons") or [],
            stored.get("simple_responses") or {"count": 0, "examples": []},
        )
    return [], {"count": 0, "examples": []}


@router.get("/api/vendor-reason-cards/{card_id}")
async def get_reason_card(
    card_id: int, session: AsyncSession = Depends(get_session)
):
    card = await get_card(session, card_id)
    if not card:
        raise HTTPException(404, "card not found")
    reasons, simple = _unwrap_reasons(card.reasons)
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
        "reasons": reasons,
        "simple_responses": simple,
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
    reasons, simple = _unwrap_reasons(card.reasons)
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
        "reasons": reasons,
        "simple_responses": simple,
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


# ----------------------------------------------------------------------------
# Per-reason "show all reviews" expansion
# ----------------------------------------------------------------------------
#
# When the user clicks a reason row in the modal, the frontend POSTs the
# list of review_ids the LLM assigned to that reason and gets back the
# full review texts. No additional LLM call — the IDs were already
# captured at extract time.

import csv  # noqa: E402
import io  # noqa: E402

from fastapi.responses import Response  # noqa: E402
from sqlalchemy import select  # noqa: E402 — local import keeps the
# top-of-file imports clean while this section is self-contained.
from app.models import Analysis, Review, VendorReasonCard  # noqa: E402


@router.get("/api/vendor-reasons/reviews-by-ids")
async def reviews_by_ids(
    ids: str = Query(..., description="comma-separated review ids"),
    session: AsyncSession = Depends(get_session),
):
    """Hydrate a list of review_ids into their full text + metadata.
    Pure DB read, no LLM call. Used by the reason-row click expand
    so the user can see the entire population of reviews that fed
    a single reason.
    """
    try:
        id_list = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "invalid ids — must be comma-separated integers")
    if not id_list:
        return {"reviews": []}
    # Safety cap. Even very loose categories rarely tag 500+ reviews to
    # a single causal reason; anything bigger is almost certainly a bug.
    if len(id_list) > 500:
        id_list = id_list[:500]

    rows = (
        await session.execute(
            select(
                Review.id,
                Review.text,
                Review.rating,
                Review.author,
                Review.posted_at,
                Review.collected_at,
                Analysis.sentiment,
                Analysis.sentiment_score,
            )
            .join(Analysis, Analysis.review_id == Review.id, isouter=True)
            .where(Review.id.in_(id_list))
        )
    ).all()

    by_id: dict[int, dict] = {}
    for rid, text, rating, author, posted, collected, sent, sscore in rows:
        s_key = sent.value if hasattr(sent, "value") else (str(sent) if sent else None)
        by_id[int(rid)] = {
            "id": int(rid),
            "text": text or "",
            "rating": int(rating) if rating is not None else None,
            "author": author or "",
            "posted_at": posted.isoformat() if posted else None,
            "collected_at": collected.isoformat() if collected else None,
            "sentiment": s_key,
            "sentiment_score": int(sscore) if sscore is not None else None,
        }
    # Preserve the request order so the user sees the same sequence the
    # LLM produced (typically rough strength-of-signal order).
    ordered = [by_id[i] for i in id_list if i in by_id]
    return {"reviews": ordered, "found": len(ordered), "requested": len(id_list)}


# ----------------------------------------------------------------------------
# CSV export
# ----------------------------------------------------------------------------


@router.get("/vendors/export.csv")
async def vendors_export_csv(
    vendor_keys: str = Query(
        "", description="comma-separated vendor keys; empty means all visible vendors"
    ),
    include: str = Query("both", pattern="^(strengths|weaknesses|both)$"),
    include_reasons: int = Query(
        0, description="1 = append saved reason analyses as a 'reasons' column"
    ),
    exclude_weak: int = Query(
        1,
        description=(
            "1 = drop rows whose polarity ratio is under 30 % (the same "
            "threshold the /vendors page grays out as a weak signal); 0 "
            "= keep every Top-5 entry. Default 1."
        ),
    ),
    session: AsyncSession = Depends(get_session),
):
    """Download the strength/weakness Top 5 table for the selected vendors
    as a CSV. The reasons column (optional) joins each row with the
    causal reasons from the matching VendorReasonCard, when one exists.

    Excel-friendly: UTF-8 with BOM so Korean characters render correctly
    when the file is opened in Excel on Windows. Filename is ASCII to
    avoid the latin-1 Content-Disposition pitfall."""
    vendors = await list_vendors(session)
    selected = {k.strip() for k in vendor_keys.split(",") if k.strip()}
    if selected:
        vendors = [v for v in vendors if v.get("key") in selected]

    # Optional: load saved reason cards so we can splice their causal
    # mechanisms into the export. Keyed by (vendor_key, lowercased
    # category_name, band) to match the same triple the page uses.
    reason_map: dict[tuple[str, str, str], list[tuple[str, int]]] = {}
    if include_reasons:
        cards = (
            await session.execute(
                select(VendorReasonCard).where(VendorReasonCard.hidden.is_(False))
            )
        ).scalars().all()
        for c in cards:
            payload = c.reasons
            if isinstance(payload, dict):
                reasons = payload.get("reasons") or []
            elif isinstance(payload, list):
                reasons = payload
            else:
                reasons = []
            entries = [
                (str(r.get("reason") or "").strip(), int(r.get("count") or 0))
                for r in reasons
                if isinstance(r, dict) and (r.get("reason") or "").strip()
            ]
            key = (c.vendor_key, (c.category_name or "").strip().lower(), c.band)
            reason_map[key] = entries

    def _join_reasons(entries: list[tuple[str, int]]) -> str:
        return "; ".join(f"{name}({n})" for name, n in entries)

    # Stream rows into an in-memory buffer; CSV files for ~14 vendors ×
    # 10 rows are tiny so this is fine without chunking.
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "vendor",
        "type",
        "category",
        "pct",
        "count",
        "wilson_score",
        "description",
        "small_sample",
        "reasons",
    ])

    # 30 % threshold matches the gray-out logic in vendors.html, so the
    # exported CSV and the on-screen "strong" rows are the same set when
    # the user keeps the default exclude_weak=1.
    WEAK_THRESHOLD = 0.30

    for v in vendors:
        if include in ("strengths", "both"):
            for s in v.get("strengths") or []:
                pos_pct = float(s.get("pos_pct") or 0.0)
                if exclude_weak and pos_pct < WEAK_THRESHOLD:
                    continue
                name = (s.get("name") or "").strip()
                key = (v.get("key"), name.lower(), "positive")
                writer.writerow([
                    v.get("display") or v.get("key"),
                    "strength",
                    name,
                    round(pos_pct * 100, 1),
                    int(s.get("total") or 0),
                    round(float(s.get("pos_score") or 0.0), 3),
                    (s.get("description") or "").strip(),
                    "Y" if s.get("small_sample") else "",
                    _join_reasons(reason_map.get(key, [])) if include_reasons else "",
                ])
        if include in ("weaknesses", "both"):
            for w in v.get("weaknesses") or []:
                neg_pct = float(w.get("neg_pct") or 0.0)
                if exclude_weak and neg_pct < WEAK_THRESHOLD:
                    continue
                name = (w.get("name") or "").strip()
                key = (v.get("key"), name.lower(), "negative")
                writer.writerow([
                    v.get("display") or v.get("key"),
                    "weakness",
                    name,
                    round(neg_pct * 100, 1),
                    int(w.get("total") or 0),
                    round(float(w.get("neg_score") or 0.0), 3),
                    (w.get("description") or "").strip(),
                    "Y" if w.get("small_sample") else "",
                    _join_reasons(reason_map.get(key, [])) if include_reasons else "",
                ])

    # BOM lets Excel-on-Windows auto-detect UTF-8 for the Korean cells.
    body = ("﻿" + buf.getvalue()).encode("utf-8")
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="vendor_analysis.csv"'
        },
    )
