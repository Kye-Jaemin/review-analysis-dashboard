"""Vendor categories — user-named groups of existing Investigation cards.

Lets the dashboard group several vendor investigation cards (e.g. several
fitness apps) under a category label (e.g. "헬스"), then scope /vendors to
just that group. Membership is a soft reference (investigation_ids is a
plain JSON list, not a FK) — same derived-reference philosophy Investigation
itself uses for source_ids/root_ids. Readers self-heal by dropping ids that
no longer resolve to a live Investigation, mirroring the pattern in
app/routes/investigations.py's list_investigations().

Kept as a standalone service (rather than inlined in a route file, the way
investigations.py does its own CRUD) because resolve_vendor_category_source_ids()
needs to be called from app/routes/vendors.py as well as
app/routes/vendor_categories.py — a route-to-route import would risk a
circular dependency.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Investigation, Review, VendorCategory


async def list_vendor_categories(
    session: AsyncSession, *, include_hidden: bool = False
) -> list[dict]:
    """Return every vendor category with a roll-up of its member
    investigation cards (self-healing dead investigation_ids on read,
    same pattern as investigations.py's list_investigations()).

    Returns:
      [{id, label, description, investigation_ids, investigations:[{id,label}],
        investigation_count, source_count, review_count, display_order,
        hidden, created_at, updated_at}, ...]
    """
    stmt = select(VendorCategory).order_by(
        VendorCategory.display_order.asc(),
        VendorCategory.updated_at.desc(),
    )
    if not include_hidden:
        stmt = stmt.where(VendorCategory.hidden.is_(False))
    rows = (await session.execute(stmt)).scalars().all()
    if not rows:
        return []

    invs = {
        i.id: i for i in (await session.execute(select(Investigation))).scalars().all()
    }
    src_count_rows = (
        await session.execute(
            select(Review.source_id, func.count(Review.id)).group_by(Review.source_id)
        )
    ).all()
    src_review_count: dict[int, int] = {sid: int(c) for sid, c in src_count_rows}

    # Same opportunistic self-heal as investigations.py: a category can
    # reference an Investigation.id that's since been deleted (JSON list,
    # not a FK, so no cascade). Strip dead ids and persist the cleanup —
    # once, at the end, off the hot path for the common clean case.
    dirty = False
    out: list[dict] = []
    for vc in rows:
        raw_ids = vc.investigation_ids or []
        live_ids = [iid for iid in raw_ids if iid in invs]
        if live_ids != raw_ids:
            vc.investigation_ids = live_ids
            dirty = True

        member_items: list[dict] = []
        source_id_union: set[int] = set()
        for iid in live_ids:
            inv = invs[iid]
            member_items.append({"id": inv.id, "label": inv.label})
            source_id_union.update(inv.source_ids or [])

        review_count = sum(src_review_count.get(sid, 0) for sid in source_id_union)

        out.append(
            {
                "id": vc.id,
                "label": vc.label,
                "description": vc.description,
                "investigation_ids": live_ids,
                "investigations": member_items,
                "investigation_count": len(live_ids),
                "source_count": len(source_id_union),
                "review_count": review_count,
                "display_order": vc.display_order or 0,
                "hidden": bool(vc.hidden),
                "created_at": vc.created_at.isoformat() if vc.created_at else None,
                "updated_at": vc.updated_at.isoformat() if vc.updated_at else None,
            }
        )

    if dirty:
        try:
            await session.commit()
        except Exception:
            await session.rollback()

    return out


async def resolve_vendor_category_source_ids(
    session: AsyncSession, vendor_category_id: Optional[int]
) -> tuple[Optional[set[int]], Optional[VendorCategory]]:
    """vendor_category_id -> (union of member investigations' source_ids, model).

    Mirrors reviews.py's _resolve_investigation() tuple-return convention
    so callers (e.g. /vendors) can distinguish "no filter" from "filter to
    zero vendors":

      - vendor_category_id is None      -> (None, None)   no filter
      - id doesn't resolve (deleted)     -> (None, None)   treat as no filter,
                                             not an error — a stale bookmark
                                             shouldn't 404 the whole page
      - investigation_ids == []          -> (set(), vc)    explicit "0 vendors"
      - normal                           -> (source_id union, vc)

    A multi-vendor member Investigation (one card spanning several Source
    rows) is handled automatically by the union — no special-casing needed.
    Hidden member Investigations are included in the union too: `hidden`
    is a dashboard *display* flag, not a statement about whether the
    vendor belongs in this category.
    """
    if vendor_category_id is None:
        return None, None
    vc = await session.get(VendorCategory, vendor_category_id)
    if vc is None:
        return None, None
    if not vc.investigation_ids:
        return set(), vc
    stmt = select(Investigation).where(Investigation.id.in_(vc.investigation_ids))
    invs = (await session.execute(stmt)).scalars().all()
    source_ids: set[int] = set()
    for inv in invs:
        source_ids.update(inv.source_ids or [])
    return source_ids, vc


async def create_vendor_category(
    session: AsyncSession,
    *,
    label: str,
    description: Optional[str] = None,
    investigation_ids: Optional[list[int]] = None,
) -> VendorCategory:
    label = (label or "").strip()
    if not label:
        raise ValueError("label is required")
    # New categories land at the end, same "max + 1" convention as
    # investigations.py's create_investigation().
    max_order = (
        await session.execute(select(func.max(VendorCategory.display_order)))
    ).scalar() or 0
    vc = VendorCategory(
        label=label[:200],
        description=(description or "").strip()[:1000] or None,
        investigation_ids=list(investigation_ids or []),
        display_order=int(max_order) + 1,
    )
    session.add(vc)
    await session.commit()
    await session.refresh(vc)
    return vc


async def update_vendor_category(
    session: AsyncSession,
    vc_id: int,
    *,
    label: Optional[str] = None,
    description: Optional[str] = None,
    investigation_ids: Optional[list[int]] = None,
) -> Optional[VendorCategory]:
    vc = await session.get(VendorCategory, vc_id)
    if not vc:
        return None
    if label is not None:
        label = label.strip()
        if not label:
            raise ValueError("label cannot be empty")
        vc.label = label[:200]
    if description is not None:
        vc.description = (description or "").strip()[:1000] or None
    if investigation_ids is not None:
        vc.investigation_ids = list(investigation_ids)
    await session.commit()
    await session.refresh(vc)
    return vc


async def set_vendor_category_hidden(
    session: AsyncSession, vc_id: int, hidden: bool
) -> Optional[VendorCategory]:
    vc = await session.get(VendorCategory, vc_id)
    if not vc:
        return None
    vc.hidden = bool(hidden)
    await session.commit()
    await session.refresh(vc)
    return vc


async def delete_vendor_category(session: AsyncSession, vc_id: int) -> bool:
    vc = await session.get(VendorCategory, vc_id)
    if not vc:
        return False
    await session.delete(vc)
    await session.commit()
    return True


async def reorder_vendor_categories(session: AsyncSession, ids: list[int]) -> int:
    """Persist the new category order. Same semantics as investigations.py's
    reorder_investigations(): client sends the full ordered id list, server
    assigns display_order = index + 1 to each row that still exists;
    unknown/duplicate ids are silently skipped."""
    seen: set[int] = set()
    order_idx = 0
    for vc_id in ids:
        if vc_id in seen:
            continue
        seen.add(vc_id)
        vc = await session.get(VendorCategory, vc_id)
        if not vc:
            continue
        order_idx += 1
        vc.display_order = order_idx
    await session.commit()
    return order_idx
