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
    return {
        "investigation_id": investigation_id,
        "categories": out,
        "has_tier_data": has_tier_data,
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
