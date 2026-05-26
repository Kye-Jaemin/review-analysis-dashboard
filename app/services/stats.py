from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Analysis, Category, Review, Sentiment, Source

SENTIMENT_ORDER = ["very_negative", "negative", "neutral", "positive", "very_positive"]


async def summary(session: AsyncSession) -> dict:
    total = (await session.execute(select(func.count(Review.id)))).scalar() or 0
    avg_rating = (await session.execute(select(func.avg(Review.rating)))).scalar()
    avg_sentiment = (await session.execute(select(func.avg(Analysis.sentiment_score)))).scalar()
    last_collected = (await session.execute(select(func.max(Review.collected_at)))).scalar()

    sent_dist: dict[str, int] = {s: 0 for s in SENTIMENT_ORDER}
    rows = (
        await session.execute(
            select(Analysis.sentiment, func.count(Analysis.id)).group_by(Analysis.sentiment)
        )
    ).all()
    for sent, c in rows:
        if sent is None:
            continue
        key = sent.value if hasattr(sent, "value") else str(sent)
        sent_dist[key] = c

    # by source
    by_source_rows = (
        await session.execute(
            select(Source.id, Source.label, func.count(Review.id))
            .join(Review, Review.source_id == Source.id)
            .group_by(Source.id, Source.label)
            .order_by(func.count(Review.id).desc())
        )
    ).all()
    by_source = [{"id": sid, "label": label, "count": c} for sid, label, c in by_source_rows]

    # by category
    by_cat_q = (
        await session.execute(
            select(Category.id, Category.path, Analysis.sentiment, func.count(Analysis.id))
            .join(Analysis, Analysis.category_id == Category.id)
            .group_by(Category.id, Category.path, Analysis.sentiment)
            .order_by(Category.path)
        )
    ).all()
    cat_map: dict[int, dict] = {}
    for cid, path, sent, c in by_cat_q:
        node = cat_map.setdefault(cid, {"id": cid, "path": path, "sentiments": {s: 0 for s in SENTIMENT_ORDER}})
        if sent is not None:
            key = sent.value if hasattr(sent, "value") else str(sent)
            node["sentiments"][key] = c
    by_category = sorted(cat_map.values(), key=lambda x: x["path"])

    # recent
    recent_rows = (
        await session.execute(
            select(Review)
            .options(selectinload(Review.source), selectinload(Review.analysis))
            .order_by(Review.collected_at.desc())
            .limit(10)
        )
    ).scalars().all()
    recent = []
    for r in recent_rows:
        recent.append({
            "id": r.id,
            "source_label": r.source.label if r.source else "—",
            "author": r.author,
            "posted_at": r.posted_at.isoformat() if r.posted_at else None,
            "text": (r.text or "")[:300],
            "sentiment": (
                r.analysis.sentiment.value
                if r.analysis and r.analysis.sentiment is not None
                else None
            ),
        })

    return {
        "total": total,
        "avg_rating": float(avg_rating) if avg_rating is not None else None,
        "avg_sentiment": float(avg_sentiment) if avg_sentiment is not None else None,
        "last_collected": last_collected.isoformat() if last_collected else None,
        "sentiment_distribution": sent_dist,
        "by_source": by_source,
        "by_category": by_category,
        "recent": recent,
    }


def _bucket_key(d: datetime, period: str) -> str:
    if period == "month":
        return d.strftime("%Y-%m")
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


async def trend(session: AsyncSession, mode: str = "distribution", period: str = "week") -> dict:
    rows = (
        await session.execute(
            select(Review.posted_at, Analysis.sentiment, Analysis.sentiment_score)
            .join(Analysis, Analysis.review_id == Review.id)
            .where(Review.posted_at.is_not(None))
        )
    ).all()

    buckets: dict[str, dict] = {}
    for posted_at, sent, score in rows:
        if posted_at is None or sent is None:
            continue
        key = _bucket_key(posted_at, period)
        b = buckets.setdefault(key, {s: 0 for s in SENTIMENT_ORDER} | {"sum_score": 0, "count": 0})
        ks = sent.value if hasattr(sent, "value") else str(sent)
        b[ks] += 1
        if score is not None:
            b["sum_score"] += score
            b["count"] += 1

    points = []
    for bucket_key in sorted(buckets.keys()):
        b = buckets[bucket_key]
        point: dict = {"bucket": bucket_key, **{s: b[s] for s in SENTIMENT_ORDER}}
        point["avg"] = (b["sum_score"] / b["count"]) if b["count"] else None
        points.append(point)
    return {"mode": mode, "period": period, "points": points}
