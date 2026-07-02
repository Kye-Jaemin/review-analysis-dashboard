"""Vendor category CRUD API.

A vendor category is a user-named group of existing Investigation cards
(e.g. "헬스" containing several fitness-app cards), used to scope /vendors
to a subset of vendors. See app/services/vendor_categories.py for the
scoping logic; this file is a thin HTTP layer over it, mirroring the
CRUD shape of app/routes/investigations.py.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.services.vendor_categories import (
    create_vendor_category,
    delete_vendor_category,
    list_vendor_categories,
    reorder_vendor_categories,
    set_vendor_category_hidden,
    update_vendor_category,
)

router = APIRouter()


class VendorCategoryIn(BaseModel):
    label: str
    description: Optional[str] = None
    investigation_ids: list[int] = []


class VendorCategoryPatch(BaseModel):
    label: Optional[str] = None
    description: Optional[str] = None
    investigation_ids: Optional[list[int]] = None


class VisibilityPayload(BaseModel):
    hidden: bool


class ReorderPayload(BaseModel):
    ids: list[int]


@router.get("/api/vendor-categories")
async def list_vendor_categories_endpoint(
    include_hidden: bool = False,
    session: AsyncSession = Depends(get_session),
):
    categories = await list_vendor_categories(session, include_hidden=include_hidden)
    return {"vendor_categories": categories}


@router.post("/api/vendor-categories")
async def create_vendor_category_endpoint(
    payload: VendorCategoryIn, session: AsyncSession = Depends(get_session)
):
    try:
        vc = await create_vendor_category(
            session,
            label=payload.label,
            description=payload.description,
            investigation_ids=payload.investigation_ids,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"id": vc.id, "label": vc.label}


@router.patch("/api/vendor-categories/{vc_id}")
async def update_vendor_category_endpoint(
    vc_id: int,
    payload: VendorCategoryPatch,
    session: AsyncSession = Depends(get_session),
):
    try:
        vc = await update_vendor_category(
            session,
            vc_id,
            label=payload.label,
            description=payload.description,
            investigation_ids=payload.investigation_ids,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    if not vc:
        raise HTTPException(404, "vendor category not found")
    return {"id": vc.id}


@router.patch("/api/vendor-categories/{vc_id}/visibility")
async def set_vendor_category_visibility(
    vc_id: int,
    payload: VisibilityPayload,
    session: AsyncSession = Depends(get_session),
):
    vc = await set_vendor_category_hidden(session, vc_id, payload.hidden)
    if not vc:
        raise HTTPException(404, "vendor category not found")
    return {"id": vc.id, "hidden": vc.hidden}


@router.delete("/api/vendor-categories/{vc_id}")
async def delete_vendor_category_endpoint(
    vc_id: int, session: AsyncSession = Depends(get_session)
):
    ok = await delete_vendor_category(session, vc_id)
    if not ok:
        raise HTTPException(404, "vendor category not found")
    return {"ok": True}


@router.post("/api/vendor-categories/reorder")
async def reorder_vendor_categories_endpoint(
    payload: ReorderPayload, session: AsyncSession = Depends(get_session)
):
    count = await reorder_vendor_categories(session, payload.ids)
    return {"ok": True, "count": count}
