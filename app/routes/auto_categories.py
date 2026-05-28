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

    # Sentiment distribution per auto category
    rows = (
        await session.execute(
            select(Analysis.auto_category_id, Analysis.sentiment, func.count(Analysis.id))
            .where(Analysis.auto_category_id.in_([c.id for c in cats]))
            .group_by(Analysis.auto_category_id, Analysis.sentiment)
        )
    ).all()
    dist: dict[int, dict[str, int]] = {c.id: {s: 0 for s in SENTIMENT_ORDER} for c in cats}
    totals: dict[int, int] = {c.id: 0 for c in cats}
    for cid, sent, cnt in rows:
        key = sent.value if hasattr(sent, "value") else str(sent) if sent else None
        if key and key in dist[cid]:
            dist[cid][key] = cnt
        totals[cid] += cnt

    out = []
    for c in cats:
        d = dist[c.id]
        out.append({
            "id": c.id,
            "name": c.name,
            "description": c.description,
            "display_order": c.display_order,
            "review_count": totals[c.id],
            "sentiments": d,
        })
    return {"investigation_id": investigation_id, "categories": out}


@router.get("/api/auto-categories/{cat_id}/reviews")
async def reviews_for_auto_category(
    cat_id: int,
    sentiment: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    """Return reviews tagged with this auto category, optionally filtered by sentiment band."""
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
            "summary": a.summary,
            "source_id": r.source_id,
        })
    return {
        "category": {"id": cat.id, "name": cat.name, "description": cat.description},
        "reviews": out,
    }
