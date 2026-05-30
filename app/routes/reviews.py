from datetime import datetime
from typing import List, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.models import Analysis, Category, Investigation, Review, Sentiment, Source
from app.services.stats import _descendants_of
from app.services.stats import summary as stats_summary
from app.services.stats import trend as stats_trend
from app.templating import render

router = APIRouter()

PAGE_SIZE = 25


def _parse_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _parse_int(s: Optional[str]) -> Optional[int]:
    """HTML <select> with an empty 'All' option sends ?source_id= which fails
    FastAPI's int parser. Accept empty/None as 'no filter'."""
    if s is None or s == "":
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _build_filter_query(
    *,
    source_id: Optional[int],
    category_id: Optional[int],
    sentiment: List[str],
    from_date: Optional[str],
    to_date: Optional[str],
    q: Optional[str],
    inv_source_ids: Optional[set[int]] = None,
    inv_category_ids: Optional[set[int]] = None,
):
    stmt = select(Review).join(Source, Source.id == Review.source_id)
    if source_id:
        stmt = stmt.where(Review.source_id == source_id)
    if inv_source_ids:
        stmt = stmt.where(Review.source_id.in_(inv_source_ids))
    if sentiment or category_id is not None or inv_category_ids:
        stmt = stmt.outerjoin(Analysis, Analysis.review_id == Review.id)
    if category_id:
        stmt = stmt.where(Analysis.category_id == category_id)
    if inv_category_ids:
        stmt = stmt.where(Analysis.category_id.in_(inv_category_ids))
    if sentiment:
        sentiment_enums = []
        for s in sentiment:
            try:
                sentiment_enums.append(Sentiment(s))
            except ValueError:
                pass
        if sentiment_enums:
            stmt = stmt.where(Analysis.sentiment.in_(sentiment_enums))
    fd = _parse_date(from_date)
    td = _parse_date(to_date)
    if fd:
        stmt = stmt.where(Review.posted_at >= fd)
    if td:
        stmt = stmt.where(Review.posted_at <= td)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Review.text.ilike(like), Review.author.ilike(like)))
    return stmt


async def _resolve_investigation(
    session: AsyncSession, inv_id: Optional[int]
) -> tuple[Optional[set[int]], Optional[set[int]], Optional[Investigation]]:
    """Look up an Investigation and return (source_ids_set, descendant_category_ids_set, model).
    Either set is None when the card doesn't constrain on that axis."""
    if inv_id is None:
        return None, None, None
    inv = await session.get(Investigation, inv_id)
    if inv is None:
        return None, None, None
    src_set = set(inv.source_ids or []) or None
    cat_set: Optional[set[int]] = None
    if inv.root_ids:
        all_cats = (await session.execute(select(Category))).scalars().all()
        parent_by_id = {c.id: c.parent_id for c in all_cats}
        cat_set = _descendants_of(parent_by_id, inv.root_ids) or None
    return src_set, cat_set, inv


@router.get("/reviews")
async def list_reviews(
    request: Request,
    page: int = Query(1, ge=1),
    source_id: Optional[str] = None,
    category_id: Optional[str] = None,
    sentiment: List[str] = Query(default_factory=list),
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    q: Optional[str] = None,
    investigation_id: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    source_id_i = _parse_int(source_id)
    category_id_i = _parse_int(category_id)
    inv_id = _parse_int(investigation_id)
    # Drop empty multi-checkbox values that some browsers append.
    sentiment = [s for s in sentiment if s]

    inv_src, inv_cat, active_inv = await _resolve_investigation(session, inv_id)

    base_stmt = _build_filter_query(
        source_id=source_id_i, category_id=category_id_i, sentiment=sentiment,
        from_date=from_date, to_date=to_date, q=q,
        inv_source_ids=inv_src, inv_category_ids=inv_cat,
    )

    total = (
        await session.execute(select(func.count()).select_from(base_stmt.subquery()))
    ).scalar() or 0
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, total_pages)

    stmt = (
        base_stmt
        .options(
            selectinload(Review.source),
            selectinload(Review.analysis).selectinload(Analysis.category),
        )
        .order_by(Review.posted_at.desc().nullslast(), Review.id.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    )
    rows = (await session.execute(stmt)).scalars().unique().all()

    sources = (await session.execute(select(Source).order_by(Source.label))).scalars().all()
    categories = (await session.execute(select(Category).order_by(Category.path))).scalars().all()

    # Investigation cards for the top row.
    inv_rows = (
        await session.execute(select(Investigation).order_by(Investigation.updated_at.desc()))
    ).scalars().all()
    source_map = {s.id: s for s in sources}
    cat_map = {c.id: c for c in categories}
    # Per-source review counts so each card can show its grand total too.
    src_count_rows = (
        await session.execute(
            select(Review.source_id, func.count(Review.id)).group_by(Review.source_id)
        )
    ).all()
    src_review_count: dict[int, int] = {sid: int(c) for sid, c in src_count_rows}
    investigations = []
    for inv in inv_rows:
        inv_src_items = []
        total_reviews = 0
        for sid in inv.source_ids or []:
            s = source_map.get(sid)
            if s:
                cnt = src_review_count.get(s.id, 0)
                total_reviews += cnt
                inv_src_items.append({
                    "id": s.id, "label": s.label,
                    "type": s.type.value if hasattr(s.type, "value") else str(s.type),
                    "icon_url": s.icon_url,
                    "review_count": cnt,
                })
        inv_root_items = []
        for cid in inv.root_ids or []:
            c = cat_map.get(cid)
            if c:
                inv_root_items.append({"id": c.id, "name": c.name})
        investigations.append({
            "id": inv.id, "label": inv.label,
            "sources": inv_src_items, "roots": inv_root_items,
            "review_count": total_reviews,
            "updated_at": inv.updated_at.isoformat() if inv.updated_at else None,
        })

    filters = {
        "source_id": source_id_i, "category_id": category_id_i, "sentiment": sentiment,
        "from_date": from_date, "to_date": to_date, "q": q,
    }

    qs_pairs = []
    if source_id_i is not None: qs_pairs.append(("source_id", source_id_i))
    if category_id_i is not None: qs_pairs.append(("category_id", category_id_i))
    for s in sentiment or []:
        qs_pairs.append(("sentiment", s))
    if from_date: qs_pairs.append(("from_date", from_date))
    if to_date: qs_pairs.append(("to_date", to_date))
    if q: qs_pairs.append(("q", q))
    if inv_id is not None: qs_pairs.append(("investigation_id", inv_id))
    qs = urlencode(qs_pairs)

    def pagination_qs(p: int) -> str:
        return urlencode(qs_pairs + [("page", p)])

    return render(
        request,
        "reviews.html",
        reviews=rows,
        sources=sources,
        investigations=investigations,
        active_investigation_id=inv_id,
        categories=categories,
        filters=filters,
        page=page,
        total_pages=total_pages,
        qs=qs,
        pagination_qs=pagination_qs,
    )


@router.get("/api/stats")
async def api_stats(
    source_ids: List[str] = Query(default_factory=list),
    root_ids: List[str] = Query(default_factory=list),
    investigation_id: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
):
    s_ids = [v for v in (_parse_int(s) for s in source_ids) if v is not None]
    r_ids = [v for v in (_parse_int(s) for s in root_ids) if v is not None]
    inv_id = _parse_int(investigation_id)
    return await stats_summary(
        session,
        source_ids=s_ids or None,
        root_ids=r_ids or None,
        investigation_id=inv_id,
    )


@router.get("/api/stats/trend")
async def api_stats_trend(
    mode: str = "distribution",
    period: str = "week",
    source_ids: List[str] = Query(default_factory=list),
    root_ids: List[str] = Query(default_factory=list),
    session: AsyncSession = Depends(get_session),
):
    s_ids = [v for v in (_parse_int(s) for s in source_ids) if v is not None]
    r_ids = [v for v in (_parse_int(s) for s in root_ids) if v is not None]
    return await stats_trend(
        session, mode=mode, period=period, source_ids=s_ids or None, root_ids=r_ids or None
    )
