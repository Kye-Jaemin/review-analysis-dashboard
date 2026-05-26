from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Analysis, Category, Review, Source

SENTIMENT_ORDER = ["very_negative", "negative", "neutral", "positive", "very_positive"]


def _enum_str(v) -> str:
    return v.value if hasattr(v, "value") else str(v)


def _normalize_ids(ids: Optional[Sequence[int]]) -> Optional[list[int]]:
    if not ids:
        return None
    cleaned = [int(s) for s in ids if s is not None]
    return cleaned or None


def _descendants_of(parent_by_id: dict[int, Optional[int]], root_ids: Sequence[int]) -> set[int]:
    """All category IDs in the subtree rooted at any of the given root_ids,
    inclusive of the roots themselves."""
    children: dict[Optional[int], list[int]] = {}
    for cid, pid in parent_by_id.items():
        children.setdefault(pid, []).append(cid)
    result: set[int] = set()
    stack = list(root_ids)
    while stack:
        cid = stack.pop()
        if cid in result:
            continue
        result.add(cid)
        stack.extend(children.get(cid, []))
    return result


def _join_analysis_to_review(stmt: Select) -> Select:
    return stmt.join(Analysis, Analysis.review_id == Review.id)


def _apply_review_filter(
    stmt: Select,
    source_ids: Optional[list[int]],
    category_ids: Optional[set[int]],
) -> Select:
    """For Review-rooted queries. When a category filter is present, restrict
    to reviews whose Analysis falls in the selected subtree (join on Analysis)."""
    if category_ids is not None:
        stmt = _join_analysis_to_review(stmt).where(Analysis.category_id.in_(category_ids))
    if source_ids:
        stmt = stmt.where(Review.source_id.in_(source_ids))
    return stmt


def _apply_analysis_filter(
    stmt: Select,
    source_ids: Optional[list[int]],
    category_ids: Optional[set[int]],
) -> Select:
    """For Analysis-rooted queries. Joins Review only when needed (source filter)."""
    if source_ids:
        stmt = stmt.join(Review, Review.id == Analysis.review_id).where(
            Review.source_id.in_(source_ids)
        )
    if category_ids is not None:
        stmt = stmt.where(Analysis.category_id.in_(category_ids))
    return stmt


async def summary(
    session: AsyncSession,
    source_ids: Optional[Sequence[int]] = None,
    root_ids: Optional[Sequence[int]] = None,
) -> dict:
    src_ids = _normalize_ids(source_ids)
    selected_roots = _normalize_ids(root_ids)

    # Load all categories — needed for tree rollup and to build descendant sets.
    all_cats = (await session.execute(select(Category))).scalars().all()
    parent_by_id: dict[int, Optional[int]] = {c.id: c.parent_id for c in all_cats}
    name_by_id: dict[int, str] = {c.id: c.name for c in all_cats}
    roots = [c for c in all_cats if c.parent_id is None]
    all_roots = [{"id": c.id, "name": c.name} for c in sorted(roots, key=lambda x: x.name)]

    # If root filter is active, build the descendant set to filter analyses by.
    cat_filter: Optional[set[int]] = None
    if selected_roots:
        cat_filter = _descendants_of(parent_by_id, selected_roots)
        if not cat_filter:
            # Shouldn't happen, but bail safe
            cat_filter = set()

    # Full source list for chip UI (always unfiltered).
    all_sources_rows = (
        await session.execute(
            select(Source.id, Source.label, Source.type, Source.icon_url).order_by(Source.label)
        )
    ).all()
    all_sources = [
        {"id": sid, "label": label, "type": _enum_str(t), "icon_url": icon}
        for sid, label, t, icon in all_sources_rows
    ]

    total = (
        await session.execute(_apply_review_filter(select(func.count(Review.id)), src_ids, cat_filter))
    ).scalar() or 0
    avg_rating = (
        await session.execute(_apply_review_filter(select(func.avg(Review.rating)), src_ids, cat_filter))
    ).scalar()
    last_collected = (
        await session.execute(
            _apply_review_filter(select(func.max(Review.collected_at)), src_ids, cat_filter)
        )
    ).scalar()

    avg_sentiment = (
        await session.execute(
            _apply_analysis_filter(select(func.avg(Analysis.sentiment_score)), src_ids, cat_filter)
        )
    ).scalar()
    analyzed_total = (
        await session.execute(
            _apply_analysis_filter(
                select(func.count(Analysis.id)).where(Analysis.sentiment.is_not(None)),
                src_ids,
                cat_filter,
            )
        )
    ).scalar() or 0

    sent_dist: dict[str, int] = {s: 0 for s in SENTIMENT_ORDER}
    sent_stmt = select(Analysis.sentiment, func.count(Analysis.id)).group_by(Analysis.sentiment)
    sent_stmt = _apply_analysis_filter(sent_stmt, src_ids, cat_filter)
    for sent, c in (await session.execute(sent_stmt)).all():
        if sent is None:
            continue
        sent_dist[_enum_str(sent)] = c

    # by source — Source grouped, optionally constrained by source ids,
    # optionally also constrained to reviews with a matching Analysis.
    by_source_stmt = (
        select(
            Source.id,
            Source.label,
            Source.type,
            Source.display_name,
            Source.icon_url,
            func.count(Review.id),
            func.avg(Review.rating),
        )
        .join(Review, Review.source_id == Source.id)
        .group_by(Source.id, Source.label, Source.type, Source.display_name, Source.icon_url)
        .order_by(func.count(Review.id).desc())
    )
    if src_ids:
        by_source_stmt = by_source_stmt.where(Source.id.in_(src_ids))
    if cat_filter is not None:
        by_source_stmt = by_source_stmt.join(Analysis, Analysis.review_id == Review.id).where(
            Analysis.category_id.in_(cat_filter)
        )
    by_source = [
        {
            "id": sid,
            "label": label,
            "type": _enum_str(stype),
            "display_name": dname,
            "icon_url": icon,
            "count": int(c or 0),
            "avg_rating": float(ar) if ar is not None else None,
        }
        for sid, label, stype, dname, icon, c, ar in (await session.execute(by_source_stmt)).all()
    ]

    def find_root(cid: int) -> int:
        seen: set[int] = set()
        while parent_by_id.get(cid) is not None and cid not in seen:
            seen.add(cid)
            cid = parent_by_id[cid]
        return cid

    by_cat_stmt = (
        select(Category.id, Category.path, Analysis.sentiment, func.count(Analysis.id))
        .join(Analysis, Analysis.category_id == Category.id)
        .group_by(Category.id, Category.path, Analysis.sentiment)
        .order_by(Category.path)
    )
    by_cat_stmt = _apply_analysis_filter(by_cat_stmt, src_ids, cat_filter)
    cat_map: dict[int, dict] = {}
    for cid, path, sent, c in (await session.execute(by_cat_stmt)).all():
        node = cat_map.setdefault(
            cid,
            {"id": cid, "path": path, "sentiments": {s: 0 for s in SENTIMENT_ORDER}, "total": 0},
        )
        if sent is not None:
            node["sentiments"][_enum_str(sent)] = c
            node["total"] += c
    by_category = sorted(cat_map.values(), key=lambda x: x["path"])

    by_root_stmt = (
        select(Analysis.category_id, Analysis.sentiment, func.count(Analysis.id))
        .where(Analysis.category_id.is_not(None))
        .where(Analysis.sentiment.is_not(None))
        .group_by(Analysis.category_id, Analysis.sentiment)
    )
    by_root_stmt = _apply_analysis_filter(by_root_stmt, src_ids, cat_filter)
    root_sent: dict[int, dict] = {}
    for cat_id, sent, c in (await session.execute(by_root_stmt)).all():
        root_id = find_root(cat_id)
        node = root_sent.setdefault(
            root_id,
            {
                "id": root_id,
                "name": name_by_id.get(root_id, "—"),
                "sentiments": {s: 0 for s in SENTIMENT_ORDER},
                "total": 0,
            },
        )
        node["sentiments"][_enum_str(sent)] = node["sentiments"].get(_enum_str(sent), 0) + c
        node["total"] += c
    by_root_sentiment = sorted(root_sent.values(), key=lambda x: x["name"])

    recent_stmt = (
        select(Review)
        .options(selectinload(Review.source), selectinload(Review.analysis))
        .order_by(Review.collected_at.desc())
        .limit(10)
    )
    recent_stmt = _apply_review_filter(recent_stmt, src_ids, cat_filter)
    recent_rows = (await session.execute(recent_stmt)).scalars().all()
    recent = []
    for r in recent_rows:
        recent.append(
            {
                "id": r.id,
                "source_label": r.source.label if r.source else "—",
                "source_type": _enum_str(r.source.type) if r.source else None,
                "author": r.author,
                "posted_at": r.posted_at.isoformat() if r.posted_at else None,
                "rating": float(r.rating) if r.rating is not None else None,
                "text": (r.text or "")[:300],
                "sentiment": (
                    r.analysis.sentiment.value
                    if r.analysis and r.analysis.sentiment is not None
                    else None
                ),
            }
        )

    return {
        "total": total,
        "analyzed_total": int(analyzed_total),
        "avg_rating": float(avg_rating) if avg_rating is not None else None,
        "avg_sentiment": float(avg_sentiment) if avg_sentiment is not None else None,
        "last_collected": last_collected.isoformat() if last_collected else None,
        "sentiment_distribution": sent_dist,
        "by_root_sentiment": by_root_sentiment,
        "by_source": by_source,
        "by_category": by_category,
        "recent": recent,
        "all_sources": all_sources,
        "all_roots": all_roots,
        "selected_sources": src_ids or [],
        "selected_roots": selected_roots or [],
    }


def _bucket_key(d: datetime, period: str) -> str:
    if period == "month":
        return d.strftime("%Y-%m")
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


async def trend(
    session: AsyncSession,
    mode: str = "distribution",
    period: str = "week",
    source_ids: Optional[Sequence[int]] = None,
    root_ids: Optional[Sequence[int]] = None,
) -> dict:
    src_ids = _normalize_ids(source_ids)
    selected_roots = _normalize_ids(root_ids)

    cat_filter: Optional[set[int]] = None
    if selected_roots:
        all_cats = (await session.execute(select(Category))).scalars().all()
        parent_by_id = {c.id: c.parent_id for c in all_cats}
        cat_filter = _descendants_of(parent_by_id, selected_roots)

    stmt = (
        select(Review.posted_at, Analysis.sentiment, Analysis.sentiment_score)
        .join(Analysis, Analysis.review_id == Review.id)
        .where(Review.posted_at.is_not(None))
    )
    if src_ids:
        stmt = stmt.where(Review.source_id.in_(src_ids))
    if cat_filter is not None:
        stmt = stmt.where(Analysis.category_id.in_(cat_filter))
    rows = (await session.execute(stmt)).all()

    buckets: dict[str, dict] = {}
    for posted_at, sent, score in rows:
        if posted_at is None or sent is None:
            continue
        key = _bucket_key(posted_at, period)
        b = buckets.setdefault(key, {s: 0 for s in SENTIMENT_ORDER} | {"sum_score": 0, "count": 0})
        b[_enum_str(sent)] += 1
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
