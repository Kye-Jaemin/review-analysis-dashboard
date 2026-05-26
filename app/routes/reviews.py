from datetime import datetime
from typing import List, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.models import Analysis, Category, Review, Sentiment, Source
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
):
    stmt = select(Review).join(Source, Source.id == Review.source_id)
    if source_id:
        stmt = stmt.where(Review.source_id == source_id)
    if sentiment or category_id is not None:
        stmt = stmt.outerjoin(Analysis, Analysis.review_id == Review.id)
    if category_id:
        stmt = stmt.where(Analysis.category_id == category_id)
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
    session: AsyncSession = Depends(get_session),
):
    source_id_i = _parse_int(source_id)
    category_id_i = _parse_int(category_id)
    # Drop empty multi-checkbox values that some browsers append.
    sentiment = [s for s in sentiment if s]
    base_stmt = _build_filter_query(
        source_id=source_id_i, category_id=category_id_i, sentiment=sentiment,
        from_date=from_date, to_date=to_date, q=q,
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
    qs = urlencode(qs_pairs)

    def pagination_qs(p: int) -> str:
        return urlencode(qs_pairs + [("page", p)])

    return render(
        request,
        "reviews.html",
        reviews=rows,
        sources=sources,
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
    session: AsyncSession = Depends(get_session),
):
    ids = [v for v in (_parse_int(s) for s in source_ids) if v is not None]
    return await stats_summary(session, source_ids=ids or None)


@router.get("/api/stats/trend")
async def api_stats_trend(
    mode: str = "distribution",
    period: str = "week",
    source_ids: List[str] = Query(default_factory=list),
    session: AsyncSession = Depends(get_session),
):
    ids = [v for v in (_parse_int(s) for s in source_ids) if v is not None]
    return await stats_trend(session, mode=mode, period=period, source_ids=ids or None)
