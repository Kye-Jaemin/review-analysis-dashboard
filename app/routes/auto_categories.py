from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Analysis, AutoCategory, Investigation, Review, Sentiment

router = APIRouter()

SENTIMENT_ORDER = ["very_negative", "negative", "neutral", "positive", "very_positive"]


@router.get("/api/auto-categories")
async def list_auto_categories(
    investigation_id: int = Query(...),
    session: AsyncSession = Depends(get_session),
):
    """Per-card list of auto categories with sentiment breakdown."""
    inv = await session.get(Investigation, investigation_id)
    if not inv:
        raise HTTPException(404, "investigation not found")

    cats = (
        await session.execute(
            select(AutoCategory)
            .where(AutoCategory.investigation_id == investigation_id)
            .order_by(AutoCategory.display_order)
        )
    ).scalars().all()
    if not cats:
        return {"investigation_id": investigation_id, "categories": []}

    # Sentiment × user_tier distribution per auto category.
    rows = (
        await session.execute(
            select(
                Analysis.auto_category_id,
                Analysis.sentiment,
                Analysis.user_tier,
                func.count(Analysis.id),
            )
            .where(Analysis.auto_category_id.in_([c.id for c in cats]))
            .group_by(Analysis.auto_category_id, Analysis.sentiment, Analysis.user_tier)
        )
    ).all()

    def _empty_tier_dict() -> dict:
        return {s: 0 for s in SENTIMENT_ORDER}

    dist_all: dict[int, dict[str, int]] = {c.id: _empty_tier_dict() for c in cats}
    dist_by_tier: dict[int, dict[str, dict[str, int]]] = {
        c.id: {"paid": _empty_tier_dict(), "free": _empty_tier_dict(), "unknown": _empty_tier_dict()}
        for c in cats
    }
    totals_all: dict[int, int] = {c.id: 0 for c in cats}
    totals_by_tier: dict[int, dict[str, int]] = {
        c.id: {"paid": 0, "free": 0, "unknown": 0} for c in cats
    }
    has_tier_data = False

    for cid, sent, tier, cnt in rows:
        s_key = sent.value if hasattr(sent, "value") else (sent if sent else None)
        if s_key and s_key in dist_all[cid]:
            dist_all[cid][s_key] += cnt
        totals_all[cid] += cnt

        tier_key = tier if tier in ("paid", "free", "unknown") else None
        if tier_key is not None:
            has_tier_data = True
            if s_key and s_key in dist_by_tier[cid][tier_key]:
                dist_by_tier[cid][tier_key][s_key] += cnt
            totals_by_tier[cid][tier_key] += cnt

    out = []
    for c in cats:
        out.append({
            "id": c.id,
            "name": c.name,
            "description": c.description,
            "display_order": c.display_order,
            "review_count": totals_all[c.id],
            "sentiments": dist_all[c.id],
            "by_tier": {
                "paid": {"count": totals_by_tier[c.id]["paid"], "sentiments": dist_by_tier[c.id]["paid"]},
                "free": {"count": totals_by_tier[c.id]["free"], "sentiments": dist_by_tier[c.id]["free"]},
                "unknown": {"count": totals_by_tier[c.id]["unknown"], "sentiments": dist_by_tier[c.id]["unknown"]},
            },
        })

    # ---- Other bucket ----
    # "Other" should account for EVERY review in scope that didn't end up in
    # the Top 10. That includes:
    #   (a) analyses with auto_category_id IS NULL (LLM skipped, threshold
    #       cut it, out-of-range category_index, etc.) — these still carry
    #       sentiment and user_tier when classification succeeded.
    #   (b) reviews with no Analysis row at all (analysis never finished,
    #       failed during INSERT, source still collecting, etc.) — no
    #       sentiment / tier metadata available.
    # The header total must equal the actual review count in scope so the
    # math the user does (Top 10 + Other = Total) is honest.
    src_ids = inv.source_ids or []
    other_sentiments = _empty_tier_dict()
    other_by_tier = {
        "paid": {"count": 0, "sentiments": _empty_tier_dict()},
        "free": {"count": 0, "sentiments": _empty_tier_dict()},
        "unknown": {"count": 0, "sentiments": _empty_tier_dict()},
    }
    classified_other = 0  # (a) — has Analysis but auto_category_id is NULL

    if src_ids:
        other_rows = (
            await session.execute(
                select(Analysis.sentiment, Analysis.user_tier, func.count(Analysis.id))
                .join(Review, Review.id == Analysis.review_id)
                .where(Review.source_id.in_(src_ids))
                .where(Analysis.auto_category_id.is_(None))
                .group_by(Analysis.sentiment, Analysis.user_tier)
            )
        ).all()
        for sent, tier, cnt in other_rows:
            s_key = sent.value if hasattr(sent, "value") else (sent if sent else None)
            classified_other += cnt
            if s_key and s_key in other_sentiments:
                other_sentiments[s_key] += cnt
            tier_key = tier if tier in ("paid", "free", "unknown") else None
            if tier_key is not None:
                other_by_tier[tier_key]["count"] += cnt
                if s_key and s_key in other_by_tier[tier_key]["sentiments"]:
                    other_by_tier[tier_key]["sentiments"][s_key] += cnt

    # Total reviews collected in scope — the number the user sees on the
    # investigation card and expects everything to add up to.
    total_in_scope = 0
    if src_ids:
        total_in_scope = (
            await session.execute(
                select(func.count(Review.id)).where(Review.source_id.in_(src_ids))
            )
        ).scalar() or 0

    top10_sum = sum(totals_all.values())
    other_total = max(0, total_in_scope - top10_sum)
    unanalyzed_count = max(0, other_total - classified_other)

    # Convenience totals — grand_total_all anchors on the raw review count.
    grand_total_all = total_in_scope
    grand_total_by_tier = {
        "paid": sum(totals_by_tier[c.id]["paid"] for c in cats) + other_by_tier["paid"]["count"],
        "free": sum(totals_by_tier[c.id]["free"] for c in cats) + other_by_tier["free"]["count"],
        "unknown": sum(totals_by_tier[c.id]["unknown"] for c in cats) + other_by_tier["unknown"]["count"],
    }

    return {
        "investigation_id": investigation_id,
        "categories": out,
        "other": {
            "count": other_total,
            "classified_count": classified_other,
            "unanalyzed_count": unanalyzed_count,
            "sentiments": other_sentiments,
            "by_tier": other_by_tier,
        },
        "totals": {
            "all": grand_total_all,
            "paid": grand_total_by_tier["paid"],
            "free": grand_total_by_tier["free"],
            "unknown": grand_total_by_tier["unknown"],
        },
        "has_tier_data": has_tier_data,
    }


@router.get("/api/auto-categories/other/reviews")
async def reviews_for_other(
    investigation_id: int = Query(...),
    sentiment: Optional[str] = Query(None),
    user_tier: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    """Reviews in the investigation's source scope that aren't in the Top 10:
    analyses with auto_category_id IS NULL plus reviews without any Analysis
    row. The latter only show up when no sentiment / tier filter is applied."""
    inv = await session.get(Investigation, investigation_id)
    if not inv:
        raise HTTPException(404, "investigation not found")
    src_ids = inv.source_ids or []
    if not src_ids:
        return {"category": {"id": "other", "name": "Other", "description": None}, "reviews": []}

    # First the classified-but-uncategorized reviews.
    stmt = (
        select(Review, Analysis)
        .join(Analysis, Analysis.review_id == Review.id)
        .where(Review.source_id.in_(src_ids))
        .where(Analysis.auto_category_id.is_(None))
    )
    sentiment_filter = sentiment
    tier_filter = user_tier if user_tier in ("paid", "free", "unknown") else None
    if sentiment_filter:
        try:
            stmt = stmt.where(Analysis.sentiment == Sentiment(sentiment_filter))
        except ValueError:
            pass
    if tier_filter is not None:
        stmt = stmt.where(Analysis.user_tier == tier_filter)
    stmt = stmt.order_by(Review.collected_at.desc()).limit(limit)
    rows = (await session.execute(stmt)).all()
    out = []
    for r, a in rows:
        out.append({
            "id": r.id,
            "author": r.author,
            "posted_at": r.posted_at.isoformat() if r.posted_at else None,
            "rating": float(r.rating) if r.rating is not None else None,
            "text": (r.text or "")[:500],
            "sentiment": a.sentiment.value if a.sentiment else None,
            "sentiment_score": a.sentiment_score,
            "user_tier": a.user_tier,
            "summary": a.summary,
            "source_id": r.source_id,
        })

    # Reviews without any Analysis row at all — only meaningful when neither
    # sentiment nor tier filter is set, because those reviews have neither.
    if not sentiment_filter and tier_filter is None and len(out) < limit:
        remaining = limit - len(out)
        unanalyzed_stmt = (
            select(Review)
            .outerjoin(Analysis, Analysis.review_id == Review.id)
            .where(Review.source_id.in_(src_ids))
            .where(Analysis.id.is_(None))
            .order_by(Review.collected_at.desc())
            .limit(remaining)
        )
        for r in (await session.execute(unanalyzed_stmt)).scalars().all():
            out.append({
                "id": r.id,
                "author": r.author,
                "posted_at": r.posted_at.isoformat() if r.posted_at else None,
                "rating": float(r.rating) if r.rating is not None else None,
                "text": (r.text or "")[:500],
                "sentiment": None,
                "sentiment_score": None,
                "user_tier": None,
                "summary": None,
                "source_id": r.source_id,
                "unanalyzed": True,
            })

    return {
        "category": {"id": "other", "name": "Other", "description": None},
        "reviews": out,
    }


@router.get("/api/auto-categories/{cat_id}/reviews")
async def reviews_for_auto_category(
    cat_id: int,
    sentiment: Optional[str] = Query(None),
    user_tier: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    """Return reviews tagged with this auto category, optionally filtered by
    sentiment band and/or beta paid/free user_tier."""
    cat = await session.get(AutoCategory, cat_id)
    if not cat:
        raise HTTPException(404, "auto category not found")

    stmt = (
        select(Review, Analysis)
        .join(Analysis, Analysis.review_id == Review.id)
        .where(Analysis.auto_category_id == cat_id)
    )
    if sentiment:
        try:
            stmt = stmt.where(Analysis.sentiment == Sentiment(sentiment))
        except ValueError:
            pass
    if user_tier in ("paid", "free", "unknown"):
        stmt = stmt.where(Analysis.user_tier == user_tier)
    stmt = stmt.order_by(Review.collected_at.desc()).limit(limit)

    rows = (await session.execute(stmt)).all()
    out = []
    for r, a in rows:
        out.append({
            "id": r.id,
            "author": r.author,
            "posted_at": r.posted_at.isoformat() if r.posted_at else None,
            "rating": float(r.rating) if r.rating is not None else None,
            "text": (r.text or "")[:500],
            "sentiment": a.sentiment.value if a.sentiment else None,
            "sentiment_score": a.sentiment_score,
            "user_tier": a.user_tier,
            "summary": a.summary,
            "source_id": r.source_id,
        })
    return {
        "category": {"id": cat.id, "name": cat.name, "description": cat.description},
        "reviews": out,
    }
