from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.routes.reviews import _parse_int
from app.services.themes import extract_themes

router = APIRouter()


@router.post("/api/themes")
async def themes_endpoint(
    sentiment: str = Query(...),
    source_ids: List[str] = Query(default_factory=list),
    root_ids: List[str] = Query(default_factory=list),
    summary_lang: str = Query("en"),
    force: bool = Query(False),
    session: AsyncSession = Depends(get_session),
):
    s_ids = [v for v in (_parse_int(s) for s in source_ids) if v is not None]
    r_ids = [v for v in (_parse_int(s) for s in root_ids) if v is not None]
    try:
        return await extract_themes(
            session,
            sentiment=sentiment,
            source_ids=s_ids or None,
            root_ids=r_ids or None,
            summary_lang=summary_lang,
            force=force,
        )
    except Exception as e:
        raise HTTPException(500, f"theme extraction failed: {e}")
