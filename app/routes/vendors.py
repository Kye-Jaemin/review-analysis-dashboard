"""Vendor analysis page.

Server-rendered listing of every "vendor" (sources grouped by their
display name stem) with a sentiment roll-up and the auto-categories
ranked as strengths and weaknesses. Pure aggregation over data the
user already paid for — no LLM calls fire from this route."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.services.vendors import list_vendors
from app.templating import render

router = APIRouter()


@router.get("/vendors")
async def vendors_page(request: Request, session: AsyncSession = Depends(get_session)):
    vendors = await list_vendors(session)
    return render(request, "vendors.html", vendors=vendors)
