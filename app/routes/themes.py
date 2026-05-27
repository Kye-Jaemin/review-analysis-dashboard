from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import ThemeSnapshot
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


class SnapshotIn(BaseModel):
    label: str
    sentiment: str
    source_ids: list[int] = []
    root_ids: list[int] = []
    summary_lang: str = "en"
    sample_size: int = 0
    model: Optional[str] = None
    themes: list = []
    categories: list = []


@router.post("/api/themes/snapshots")
async def save_snapshot(payload: SnapshotIn, session: AsyncSession = Depends(get_session)):
    label = (payload.label or "").strip()
    if not label:
        raise HTTPException(400, "label is required")
    if not payload.themes and not payload.categories:
        raise HTTPException(400, "themes or categories payload is required")
    # We persist whichever structure carries the richer data. New flow sends
    # `categories`; if only `themes` is present (legacy / flat), keep that.
    stored = payload.categories if payload.categories else payload.themes
    snap = ThemeSnapshot(
        label=label[:200],
        sentiment=payload.sentiment,
        source_ids=payload.source_ids or [],
        root_ids=payload.root_ids or [],
        summary_lang=payload.summary_lang,
        sample_size=payload.sample_size,
        model=payload.model,
        themes=stored,
    )
    session.add(snap)
    await session.commit()
    return {"id": snap.id, "label": snap.label}


@router.get("/api/themes/snapshots")
async def list_snapshots(session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(select(ThemeSnapshot).order_by(ThemeSnapshot.id.desc()))
    ).scalars().all()
    return {
        "snapshots": [
            {
                "id": r.id,
                "label": r.label,
                "sentiment": r.sentiment,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "sample_size": r.sample_size,
                "model": r.model,
                "source_ids": r.source_ids or [],
                "root_ids": r.root_ids or [],
            }
            for r in rows
        ]
    }


@router.get("/api/themes/snapshots/{snap_id}")
async def get_snapshot(snap_id: int, session: AsyncSession = Depends(get_session)):
    snap = await session.get(ThemeSnapshot, snap_id)
    if not snap:
        raise HTTPException(404)
    raw = snap.themes or []
    # Detect storage format: list of {category, themes:[...]} → grouped (new).
    # Otherwise treat as flat themes list (legacy).
    is_grouped = (
        isinstance(raw, list)
        and raw
        and isinstance(raw[0], dict)
        and "category" in raw[0]
        and isinstance(raw[0].get("themes"), list)
    )
    if is_grouped:
        categories = raw
        themes = [
            {**t, "category": cat.get("category")}
            for cat in raw for t in (cat.get("themes") or [])
        ]
    else:
        categories = []
        themes = raw if isinstance(raw, list) else []
    return {
        "id": snap.id,
        "label": snap.label,
        "sentiment": snap.sentiment,
        "source_ids": snap.source_ids or [],
        "root_ids": snap.root_ids or [],
        "summary_lang": snap.summary_lang,
        "sample_size": snap.sample_size,
        "model": snap.model,
        "categories": categories,
        "themes": themes,
        "created_at": snap.created_at.isoformat() if snap.created_at else None,
    }


@router.delete("/api/themes/snapshots/{snap_id}")
async def delete_snapshot(snap_id: int, session: AsyncSession = Depends(get_session)):
    snap = await session.get(ThemeSnapshot, snap_id)
    if not snap:
        raise HTTPException(404)
    await session.delete(snap)
    await session.commit()
    return {"ok": True}
