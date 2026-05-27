from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.models import Analysis, Review
from app.routes.reviews import _build_filter_query, _parse_int, _resolve_investigation
from app.services import exporter

router = APIRouter()


@router.get("/export")
async def export_reviews(
    format: str = Query("csv", pattern="^(csv|json|xlsx)$"),
    source_id: Optional[str] = None,
    category_id: Optional[str] = None,
    sentiment: List[str] = Query(default_factory=list),
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    q: Optional[str] = None,
    investigation_id: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    sentiment = [s for s in sentiment if s]
    inv_src, inv_cat, _inv = await _resolve_investigation(session, _parse_int(investigation_id))
    stmt = _build_filter_query(
        source_id=_parse_int(source_id), category_id=_parse_int(category_id), sentiment=sentiment,
        from_date=from_date, to_date=to_date, q=q,
        inv_source_ids=inv_src, inv_category_ids=inv_cat,
    ).options(
        selectinload(Review.source),
        selectinload(Review.analysis).selectinload(Analysis.category),
    )
    rows = (await session.execute(stmt)).scalars().unique().all()

    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    if format == "csv":
        body = exporter.to_csv(rows)
        return Response(
            content=body,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="reviews-{ts}.csv"'},
        )
    if format == "json":
        body = exporter.to_json(rows)
        return Response(
            content=body,
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="reviews-{ts}.json"'},
        )
    # xlsx
    body = exporter.to_xlsx(rows)
    return Response(
        content=body,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="reviews-{ts}.xlsx"'},
    )
