from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Category, Investigation, Source

router = APIRouter()


class InvestigationIn(BaseModel):
    label: str
    description: Optional[str] = None
    source_ids: list[int] = []
    root_ids: list[int] = []


class InvestigationPatch(BaseModel):
    label: Optional[str] = None
    description: Optional[str] = None
    source_ids: Optional[list[int]] = None
    root_ids: Optional[list[int]] = None


@router.get("/api/investigations")
async def list_investigations(session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(
            select(Investigation).order_by(Investigation.updated_at.desc())
        )
    ).scalars().all()

    sources = {
        s.id: s for s in (await session.execute(select(Source))).scalars().all()
    }
    cats = {c.id: c for c in (await session.execute(select(Category))).scalars().all()}

    out = []
    for inv in rows:
        src_items = []
        for sid in inv.source_ids or []:
            s = sources.get(sid)
            if s:
                src_items.append(
                    {
                        "id": s.id,
                        "label": s.label,
                        "type": s.type.value if hasattr(s.type, "value") else str(s.type),
                        "icon_url": s.icon_url,
                    }
                )
        cat_items = []
        for cid in inv.root_ids or []:
            c = cats.get(cid)
            if c:
                cat_items.append({"id": c.id, "name": c.name})
        out.append(
            {
                "id": inv.id,
                "label": inv.label,
                "description": inv.description,
                "source_ids": inv.source_ids or [],
                "root_ids": inv.root_ids or [],
                "sources": src_items,
                "roots": cat_items,
                "created_at": inv.created_at.isoformat() if inv.created_at else None,
                "updated_at": inv.updated_at.isoformat() if inv.updated_at else None,
            }
        )
    return {"investigations": out}


@router.post("/api/investigations")
async def create_investigation(
    payload: InvestigationIn, session: AsyncSession = Depends(get_session)
):
    label = (payload.label or "").strip()
    if not label:
        raise HTTPException(400, "label is required")
    inv = Investigation(
        label=label[:200],
        description=(payload.description or "").strip()[:1000] or None,
        source_ids=payload.source_ids or [],
        root_ids=payload.root_ids or [],
    )
    session.add(inv)
    await session.commit()
    return {"id": inv.id, "label": inv.label}


@router.patch("/api/investigations/{inv_id}")
async def update_investigation(
    inv_id: int,
    payload: InvestigationPatch,
    session: AsyncSession = Depends(get_session),
):
    inv = await session.get(Investigation, inv_id)
    if not inv:
        raise HTTPException(404)
    if payload.label is not None:
        inv.label = payload.label.strip()[:200]
    if payload.description is not None:
        inv.description = (payload.description or "").strip()[:1000] or None
    if payload.source_ids is not None:
        inv.source_ids = payload.source_ids
    if payload.root_ids is not None:
        inv.root_ids = payload.root_ids
    await session.commit()
    return {"id": inv.id}


@router.delete("/api/investigations/{inv_id}")
async def delete_investigation(
    inv_id: int, session: AsyncSession = Depends(get_session)
):
    inv = await session.get(Investigation, inv_id)
    if not inv:
        raise HTTPException(404)
    await session.delete(inv)
    await session.commit()
    return {"ok": True}
