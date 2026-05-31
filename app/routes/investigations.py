from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import (
    Analysis,
    AnalysisStatus,
    AutoCategory,
    Category,
    Investigation,
    ReviewAutoCategoryLink,
    ReviewManualCategoryLink,
    Review,
    Sentiment,
    Source,
    SourceType,
    ThemeSnapshot,
)
from app.services.stats import _descendants_of

router = APIRouter()


# ---------- shared serialization helpers ----------

EXPORT_VERSION = 1

CATEGORY_COLS = ["id", "parent_id", "name", "description", "path"]
SOURCE_COLS = ["id", "type", "label", "display_name", "icon_url", "config", "created_at"]
REVIEW_COLS = [
    "id", "source_id", "external_id", "author", "posted_at", "rating",
    "text", "url", "raw", "collected_at",
]
ANALYSIS_COLS = [
    "id", "review_id", "category_id", "sentiment", "sentiment_score",
    "user_tier", "confidence", "summary", "model", "analyzed_at", "status", "error",
]
SNAPSHOT_COLS = [
    "id", "investigation_id", "label", "sentiment", "source_ids", "root_ids",
    "auto_category_ids", "summary_lang", "sample_size", "model", "themes", "created_at",
]
INVESTIGATION_COLS = [
    "id", "label", "description", "source_ids", "root_ids",
    "display_order", "created_at", "updated_at",
]
AUTO_CATEGORY_COLS = [
    "id", "investigation_id", "name", "description", "review_count",
    "display_order", "language", "translations", "created_at",
]


def _serialize(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if hasattr(v, "value"):
        return v.value
    return v


def _dump(obj, cols):
    return {c: _serialize(getattr(obj, c)) for c in cols}


def _parse_dt(s):
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    try:
        return datetime.fromisoformat(str(s).rstrip("Z"))
    except Exception:
        return None


# ---------- CRUD ----------


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
    # Pull rows in a stable base order. The final ordering is computed in
    # Python below because the grouping key depends on the related Source
    # rows (display_name / label) — there's no single SQL column we can
    # ORDER BY that produces vendor-grouped + auto-first + drag-sticky
    # output cleanly. display_order remains the within-group tiebreaker.
    rows = (
        await session.execute(
            select(Investigation).order_by(
                Investigation.display_order.asc(),
                Investigation.updated_at.desc(),
            )
        )
    ).scalars().all()

    sources = {
        s.id: s for s in (await session.execute(select(Source))).scalars().all()
    }
    cats = {c.id: c for c in (await session.execute(select(Category))).scalars().all()}

    # Precompute total review counts per source so we can roll them up
    # per investigation in one pass below.
    src_count_rows = (
        await session.execute(
            select(Review.source_id, func.count(Review.id)).group_by(Review.source_id)
        )
    ).all()
    src_review_count: dict[int, int] = {sid: int(c) for sid, c in src_count_rows}

    # Some Investigation rows out there carry stale source_ids / root_ids
    # pointing at sources or categories that have since been deleted — the
    # JSON columns aren't FKs so cascade-delete doesn't touch them. The
    # symptom is the dashboard showing review_count = 0 for a card that
    # actually predates a source rename / re-collect cycle. Self-heal on
    # read: filter out dead ids and persist back so the row is clean
    # going forward.
    dirty = False
    out = []
    for inv in rows:
        live_src_ids = [sid for sid in (inv.source_ids or []) if sid in sources]
        live_root_ids = [cid for cid in (inv.root_ids or []) if cid in cats]
        if live_src_ids != (inv.source_ids or []):
            inv.source_ids = live_src_ids
            dirty = True
        if live_root_ids != (inv.root_ids or []):
            inv.root_ids = live_root_ids
            dirty = True

        src_items = []
        total_reviews = 0
        for sid in live_src_ids:
            s = sources.get(sid)
            if s:
                cnt = src_review_count.get(s.id, 0)
                total_reviews += cnt
                src_items.append(
                    {
                        "id": s.id,
                        "label": s.label,
                        "display_name": s.display_name,
                        "type": s.type.value if hasattr(s.type, "value") else str(s.type),
                        "icon_url": s.icon_url,
                        "review_count": cnt,
                    }
                )
        cat_items = []
        for cid in live_root_ids:
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
                "review_count": total_reviews,
                "display_order": inv.display_order or 0,
                "created_at": inv.created_at.isoformat() if inv.created_at else None,
                "updated_at": inv.updated_at.isoformat() if inv.updated_at else None,
            }
        )

    # ---- Final ordering: group by vendor, auto-first inside each group --
    # "Vendor" here means the app/service the card is investigating.
    # Multiple Source rows may exist for the same app across stores
    # (Google Play MyFitnessPal, App Store MyFitnessPal, Reddit MFP), so
    # we group on a signature derived from each source's `display_name`
    # (the human app title from the store API), falling back to `label`
    # when display_name is empty.
    #
    # Sort key tuple per card:
    #   (vendor_signature, vendor_first_seen_index, is_manual, display_order)
    #
    # - vendor_signature pulls same-app cards together
    # - vendor_first_seen_index keeps vendors in the order their *first*
    #   card was originally drag-sorted, so a vendor block doesn't jump
    #   when a new card lands in it
    # - is_manual: 0 for auto (root_ids empty) -> shows first, 1 for
    #   manual -> shows after
    # - display_order: user's drag order within the (vendor, type) group
    def _vendor_sig(item: dict) -> str:
        names = []
        for s in item.get("sources") or []:
            n = (s.get("display_name") or s.get("label") or "").strip().lower()
            if n:
                names.append(n)
        names.sort()
        return "|".join(names) or "(no-sources)"

    vendor_first_seen: dict[str, int] = {}
    for idx, item in enumerate(out):
        sig = _vendor_sig(item)
        if sig not in vendor_first_seen:
            vendor_first_seen[sig] = idx

    def _sort_key(item: dict):
        sig = _vendor_sig(item)
        is_manual = 1 if (item.get("root_ids") or []) else 0
        return (
            vendor_first_seen.get(sig, 10**9),
            sig,
            is_manual,
            item.get("display_order") or 0,
            item.get("id") or 0,
        )

    out.sort(key=_sort_key)
    # Persist any opportunistic cleanups we made above (orphaned source_ids
    # / root_ids stripped). One commit at the end keeps this off the
    # request hot path for clean databases.
    if dirty:
        try:
            await session.commit()
        except Exception:
            await session.rollback()
    return {"investigations": out}


@router.post("/api/investigations")
async def create_investigation(
    payload: InvestigationIn, session: AsyncSession = Depends(get_session)
):
    label = (payload.label or "").strip()
    if not label:
        raise HTTPException(400, "label is required")
    # New cards land at the end of the user's order. Default = max + 1 so
    # they don't shove existing positions around.
    max_order = (
        await session.execute(select(func.max(Investigation.display_order)))
    ).scalar() or 0
    inv = Investigation(
        label=label[:200],
        description=(payload.description or "").strip()[:1000] or None,
        source_ids=payload.source_ids or [],
        root_ids=payload.root_ids or [],
        display_order=int(max_order) + 1,
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


class ReorderPayload(BaseModel):
    ids: list[int]


@router.post("/api/investigations/reorder")
async def reorder_investigations(
    payload: ReorderPayload,
    session: AsyncSession = Depends(get_session),
):
    """Persist the new card order. Client sends the full ordered id list;
    the server assigns display_order = index + 1 to each row that exists.
    Unknown ids are silently dropped (the client may be looking at a
    stale list when another tab deleted a card).

    Atomicity matters here: when two users drag at the same time the
    last write wins, but each call must commit a consistent assignment
    rather than half-renumber on failure."""
    seen: set[int] = set()
    order_idx = 0
    for inv_id in payload.ids:
        if inv_id in seen:
            continue
        seen.add(inv_id)
        inv = await session.get(Investigation, inv_id)
        if not inv:
            continue
        order_idx += 1
        inv.display_order = order_idx
    await session.commit()
    return {"ok": True, "count": order_idx}


# ---------- Export ONE investigation card ----------


def _scrub_surrogates(obj):
    """Recursively replace lone surrogate codepoints in any string inside
    a JSON-serializable structure. Some imported labels and review text
    carry lone surrogates (`\\udcXX`) — leftovers from earlier UTF-8 decode
    errors that the database happily stored. They survive json.dumps with
    ensure_ascii=False, then crash the final `.encode("utf-8")` with
    UnicodeEncodeError ('surrogates not allowed'). The result is the
    plain-text "Internal Server Error" the user saw on manual cards
    whose label includes a Korean root name. Scrub at serialization time
    so the file always downloads cleanly; replacement char is U+FFFD."""
    if isinstance(obj, str):
        try:
            obj.encode("utf-8")
            return obj
        except UnicodeEncodeError:
            return obj.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(obj, list):
        return [_scrub_surrogates(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _scrub_surrogates(v) for k, v in obj.items()}
    return obj


@router.get("/api/investigations/{inv_id}/export")
async def export_investigation(inv_id: int, session: AsyncSession = Depends(get_session)):
    # Diagnostic wrapper — second time around. The surrogate scrub fix
    # appears not to fully address what's failing on manual cards with
    # Korean root names; let's surface the real exception in the
    # response body again so we can SEE it instead of guessing.
    import traceback as _tb
    try:
        return await _export_investigation_impl(inv_id, session)
    except HTTPException:
        raise
    except Exception as e:
        from fastapi.responses import JSONResponse
        tb_text = _tb.format_exc()
        return JSONResponse(
            status_code=500,
            content={
                "type": type(e).__name__,
                "error": str(e),
                "traceback": tb_text.splitlines()[-25:],
            },
        )


async def _export_investigation_impl(inv_id: int, session: AsyncSession):
    inv = await session.get(Investigation, inv_id)
    if not inv:
        raise HTTPException(404)

    src_ids = list(inv.source_ids or [])
    root_ids = list(inv.root_ids or [])

    # Sources referenced by the card.
    sources_payload = []
    if src_ids:
        rows = (await session.execute(select(Source).where(Source.id.in_(src_ids)))).scalars().all()
        sources_payload = [_dump(s, SOURCE_COLS) for s in rows]

    # Categories: every root + all descendants (so the imported subtree is complete).
    all_cats = (await session.execute(select(Category))).scalars().all()
    parent_by_id = {c.id: c.parent_id for c in all_cats}
    descendants: set[int] = set()
    if root_ids:
        descendants = _descendants_of(parent_by_id, root_ids)
    # Also include any ancestor paths so parent_id chains are insertable.
    # The descendant set already contains the roots themselves (cycle-safe loop).
    cats_to_include = {c.id: c for c in all_cats if c.id in descendants}
    categories_payload = [_dump(c, CATEGORY_COLS) for c in cats_to_include.values()]

    # Reviews in scope: source_id IN src_ids. If root_ids set, also limited to
    # reviews whose Analysis falls in the descendant set.
    reviews_payload = []
    analyses_payload = []
    if src_ids:
        review_rows = (
            await session.execute(select(Review).where(Review.source_id.in_(src_ids)))
        ).scalars().all()
        if descendants:
            in_scope_ids = (
                await session.execute(
                    select(Analysis.review_id)
                    .where(Analysis.review_id.in_([r.id for r in review_rows]))
                    .where(Analysis.category_id.in_(descendants))
                )
            ).scalars().all()
            scoped = set(in_scope_ids)
            review_rows = [r for r in review_rows if r.id in scoped]
        reviews_payload = [_dump(r, REVIEW_COLS) for r in review_rows]
        ids = [r.id for r in review_rows]
        if ids:
            analysis_rows = (
                await session.execute(select(Analysis).where(Analysis.review_id.in_(ids)))
            ).scalars().all()
            analyses_payload = [_dump(a, ANALYSIS_COLS) for a in analysis_rows]

    # Theme snapshots saved under this card.
    snap_rows = (
        await session.execute(
            select(ThemeSnapshot).where(ThemeSnapshot.investigation_id == inv_id)
        )
    ).scalars().all()
    snapshots_payload = [_dump(s, SNAPSHOT_COLS) for s in snap_rows]

    # Auto-category Top-10 + the simple_positive / simple_negative buckets
    # for this card, plus the per-review junction. Without these, importing
    # an auto card produced a fresh investigation with no auto categories,
    # which fell through to "manual mode" on the dashboard and showed the
    # empty-state CTA even though the card was supposed to be auto.
    auto_cat_rows = (
        await session.execute(
            select(AutoCategory).where(AutoCategory.investigation_id == inv_id)
        )
    ).scalars().all()
    auto_categories_payload = [_dump(ac, AUTO_CATEGORY_COLS) for ac in auto_cat_rows]

    # Junction rows for THIS card's auto categories only — the export is
    # scoped to one card so we don't drag along siblings.
    rac_rows: list[tuple[int, int]] = []
    if auto_cat_rows:
        rac_rows = list(
            (await session.execute(
                select(
                    ReviewAutoCategoryLink.c.review_id,
                    ReviewAutoCategoryLink.c.auto_category_id,
                ).where(
                    ReviewAutoCategoryLink.c.auto_category_id.in_(
                        [ac.id for ac in auto_cat_rows]
                    )
                )
            )).all()
        )
    review_auto_categories_payload = [
        {"review_id": rid, "auto_category_id": acid} for rid, acid in rac_rows
    ]

    # Manual junction scoped to this investigation_id.
    rmc_rows = list(
        (await session.execute(
            select(
                ReviewManualCategoryLink.c.review_id,
                ReviewManualCategoryLink.c.investigation_id,
                ReviewManualCategoryLink.c.category_id,
            ).where(ReviewManualCategoryLink.c.investigation_id == inv_id)
        )).all()
    )
    review_manual_categories_payload = [
        {"review_id": rid, "investigation_id": iid, "category_id": cid}
        for rid, iid, cid in rmc_rows
    ]

    payload = {
        "version": EXPORT_VERSION,
        "type": "investigation",
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "investigation": _dump(inv, INVESTIGATION_COLS),
        "sources": sources_payload,
        "categories": categories_payload,
        "reviews": reviews_payload,
        "analyses": analyses_payload,
        "auto_categories": auto_categories_payload,
        "review_auto_categories": review_auto_categories_payload,
        "review_manual_categories": review_manual_categories_payload,
        "theme_snapshots": snapshots_payload,
    }

    # Scrub any lone surrogates that may have wormed in via earlier bad
    # UTF-8 decodes (typical with Korean labels round-tripped through
    # collectors / form data) so the .encode("utf-8") below doesn't
    # raise UnicodeEncodeError.
    payload = _scrub_surrogates(payload)
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in _scrub_surrogates(inv.label))[:60] or "card"
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="card-{safe_name}-{ts}.json"'},
    )


# ---------- Import ONE investigation card (merge into current workspace) ----------


@router.post("/api/investigations/import")
async def import_investigation(
    file: UploadFile = File(...), session: AsyncSession = Depends(get_session)
):
    raw = await file.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise HTTPException(400, f"invalid json: {e}")
    if not isinstance(data, dict) or data.get("type") != "investigation":
        raise HTTPException(400, "not an investigation card export")

    inv_data = data.get("investigation") or {}
    if not inv_data:
        raise HTTPException(400, "missing investigation block")

    # ---- Categories: dedupe by path; topologically insert so parents land first ----
    existing_cats = (await session.execute(select(Category))).scalars().all()
    existing_by_path: dict[str, Category] = {c.path: c for c in existing_cats}
    cat_id_map: dict[int, int] = {}

    pending = list(data.get("categories") or [])
    # We loop until no progress to handle children before parents arrive.
    safety_iters = len(pending) + 2
    while pending and safety_iters > 0:
        safety_iters -= 1
        progress = False
        for cat in list(pending):
            old_parent = cat.get("parent_id")
            if old_parent is not None and old_parent not in cat_id_map:
                # Parent not mapped yet; check whether the parent is in `pending`
                # at all — if not (orphan), treat as root.
                if any(c["id"] == old_parent for c in pending):
                    continue
                old_parent = None  # orphan promoted to root

            new_parent_id = cat_id_map.get(old_parent) if old_parent else None
            name = (cat.get("name") or "").strip() or "(unnamed)"
            if new_parent_id:
                parent_row = await session.get(Category, new_parent_id)
                new_path = f"{parent_row.path} > {name}"
            else:
                new_path = name

            if new_path in existing_by_path:
                cat_id_map[cat["id"]] = existing_by_path[new_path].id
            else:
                new_cat = Category(
                    parent_id=new_parent_id,
                    name=name,
                    description=cat.get("description") or "",
                    path=new_path,
                )
                session.add(new_cat)
                await session.flush()
                cat_id_map[cat["id"]] = new_cat.id
                existing_by_path[new_path] = new_cat
            pending.remove(cat)
            progress = True
        if not progress:
            break

    # ---- Sources: dedupe against the existing workspace ----
    # Importing the same auto card twice used to create duplicate Source
    # rows ("조사가 필요한 업체가 또 생김"), and even on a first import the
    # new sources broke vendor-stack grouping because the visible
    # display_name was right but the underlying id wasn't shared with the
    # already-present BitePal/Cal AI/etc rows the user collected before.
    # Match on (type, display_name or label) — the natural identity of an
    # app row in the workspace. Reuse the existing id when we have a
    # match; otherwise insert.
    existing_sources = (await session.execute(select(Source))).scalars().all()
    def _src_key(stype, display_name, label) -> tuple[str, str]:
        # display_name is the human app title from the store API; label
        # is what the user typed. display_name wins, label is the
        # fallback so reddit / web sources still match consistently.
        return (
            stype.value if hasattr(stype, "value") else str(stype),
            ((display_name or label) or "").strip().lower(),
        )
    existing_by_src_key: dict[tuple[str, str], Source] = {
        _src_key(s.type, s.display_name, s.label): s for s in existing_sources
    }

    src_id_map: dict[int, int] = {}
    for src in data.get("sources") or []:
        try:
            stype = SourceType(src["type"])
        except (ValueError, KeyError):
            stype = SourceType.web
        key = _src_key(stype, src.get("display_name"), src.get("label"))
        existing_src = existing_by_src_key.get(key)
        if existing_src is not None:
            src_id_map[src["id"]] = existing_src.id
            continue
        new_src = Source(
            type=stype,
            label=src.get("label") or src["type"],
            display_name=src.get("display_name"),
            icon_url=src.get("icon_url"),
            config=src.get("config") or {},
            created_at=_parse_dt(src.get("created_at")) or datetime.utcnow(),
        )
        session.add(new_src)
        await session.flush()
        src_id_map[src["id"]] = new_src.id
        existing_by_src_key[key] = new_src

    # ---- Reviews: dedupe by (new_source_id, external_id) ----
    rev_id_map: dict[int, int] = {}
    for r in data.get("reviews") or []:
        new_src_id = src_id_map.get(r.get("source_id"))
        if not new_src_id:
            continue
        ext = r.get("external_id")
        existing_rev = (
            await session.execute(
                select(Review).where(
                    Review.source_id == new_src_id, Review.external_id == ext
                )
            )
        ).scalar_one_or_none()
        if existing_rev:
            rev_id_map[r["id"]] = existing_rev.id
            continue
        posted = _parse_dt(r.get("posted_at"))
        if posted is not None and posted.tzinfo is not None:
            from datetime import timezone
            posted = posted.astimezone(timezone.utc).replace(tzinfo=None)
        new_rev = Review(
            source_id=new_src_id,
            external_id=ext,
            author=r.get("author"),
            posted_at=posted,
            rating=r.get("rating"),
            text=r.get("text") or "",
            url=r.get("url"),
            raw=r.get("raw") or {},
            collected_at=_parse_dt(r.get("collected_at")) or datetime.utcnow(),
        )
        session.add(new_rev)
        await session.flush()
        rev_id_map[r["id"]] = new_rev.id

    # ---- Analyses: insert iff no analysis already exists for that review ----
    for a in data.get("analyses") or []:
        new_rev_id = rev_id_map.get(a.get("review_id"))
        if not new_rev_id:
            continue
        existing_a = (
            await session.execute(select(Analysis).where(Analysis.review_id == new_rev_id))
        ).scalar_one_or_none()
        if existing_a:
            continue
        new_cat_id = cat_id_map.get(a.get("category_id"))
        try:
            sent = Sentiment(a["sentiment"]) if a.get("sentiment") else None
        except (ValueError, KeyError):
            sent = None
        try:
            astatus = (
                AnalysisStatus(a["status"]) if a.get("status") else AnalysisStatus.succeeded
            )
        except (ValueError, KeyError):
            astatus = AnalysisStatus.succeeded
        session.add(
            Analysis(
                review_id=new_rev_id,
                category_id=new_cat_id,
                sentiment=sent,
                sentiment_score=a.get("sentiment_score"),
                confidence=a.get("confidence"),
                summary=a.get("summary"),
                model=a.get("model"),
                analyzed_at=_parse_dt(a.get("analyzed_at")) or datetime.utcnow(),
                status=astatus,
                error=a.get("error"),
            )
        )
    await session.flush()

    # ---- Investigation: insert new with remapped source/root ids ----
    # Imported cards land at the end of the user's current order so they
    # don't jostle existing positions.
    max_order = (
        await session.execute(select(func.max(Investigation.display_order)))
    ).scalar() or 0
    new_inv = Investigation(
        label=(inv_data.get("label") or "").strip()[:200] or "(imported)",
        description=inv_data.get("description"),
        source_ids=[src_id_map[s] for s in (inv_data.get("source_ids") or []) if s in src_id_map],
        root_ids=[cat_id_map[c] for c in (inv_data.get("root_ids") or []) if c in cat_id_map],
        display_order=int(max_order) + 1,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    session.add(new_inv)
    await session.flush()

    # ---- Auto categories: insert fresh, scoped to the new investigation ----
    # Without this, importing an auto card produced a fresh investigation
    # with no AutoCategory rows, which made loadAutoCategories() return
    # empty on the dashboard, _isAutoMode flipped false, and the user saw
    # the manual empty-state CTA on what should have been an auto card.
    ac_id_map: dict[int, int] = {}
    for ac in data.get("auto_categories") or []:
        new_ac = AutoCategory(
            investigation_id=new_inv.id,
            name=ac.get("name") or "(unnamed)",
            description=ac.get("description"),
            review_count=int(ac.get("review_count") or 0),
            display_order=int(ac.get("display_order") or 0),
            language=ac.get("language") or "en",
            translations=ac.get("translations") or {},
            created_at=_parse_dt(ac.get("created_at")) or datetime.utcnow(),
        )
        session.add(new_ac)
        await session.flush()
        if ac.get("id") is not None:
            ac_id_map[int(ac["id"])] = new_ac.id

    # Per-review auto-category junction, remapped through ac_id_map and
    # rev_id_map. Skip rows whose review or auto_category id we couldn't
    # resolve (typical when the source was dropped on import).
    auto_junction_rows: list[dict] = []
    seen_aj: set[tuple[int, int]] = set()
    for row in data.get("review_auto_categories") or []:
        try:
            old_rid = int(row["review_id"])
            old_acid = int(row["auto_category_id"])
        except (KeyError, TypeError, ValueError):
            continue
        new_rid = rev_id_map.get(old_rid)
        new_acid = ac_id_map.get(old_acid)
        if new_rid is None or new_acid is None:
            continue
        key = (new_rid, new_acid)
        if key in seen_aj:
            continue
        seen_aj.add(key)
        auto_junction_rows.append({"review_id": new_rid, "auto_category_id": new_acid})
    if auto_junction_rows:
        await session.execute(ReviewAutoCategoryLink.insert(), auto_junction_rows)

    # Per-review manual junction. investigation_id is pinned to the new
    # card's id (the export carried the old one but it has no meaning
    # in the importer's workspace).
    manual_junction_rows: list[dict] = []
    seen_mj: set[int] = set()
    for row in data.get("review_manual_categories") or []:
        try:
            old_rid = int(row["review_id"])
            old_cid = int(row["category_id"])
        except (KeyError, TypeError, ValueError):
            continue
        new_rid = rev_id_map.get(old_rid)
        new_cid = cat_id_map.get(old_cid)
        if new_rid is None or new_cid is None:
            continue
        if new_rid in seen_mj:
            continue
        seen_mj.add(new_rid)
        manual_junction_rows.append({
            "review_id": new_rid,
            "investigation_id": new_inv.id,
            "category_id": new_cid,
        })
    if manual_junction_rows:
        await session.execute(ReviewManualCategoryLink.insert(), manual_junction_rows)

    # ---- Theme snapshots: link to the new investigation ----
    for snap in data.get("theme_snapshots") or []:
        # Auto-category-id references in saved mind maps need to be
        # remapped through ac_id_map too. Drop any unresolved ids
        # silently — they refer to deleted scopes on the source workspace.
        snap_auto_ids = []
        for aid in (snap.get("auto_category_ids") or []):
            mapped = ac_id_map.get(int(aid) if isinstance(aid, (int, str)) and str(aid).lstrip("-").isdigit() else None)
            if mapped is not None:
                snap_auto_ids.append(mapped)
        session.add(
            ThemeSnapshot(
                investigation_id=new_inv.id,
                label=snap.get("label") or "(unnamed)",
                sentiment=snap.get("sentiment") or "neutral",
                source_ids=[src_id_map[x] for x in (snap.get("source_ids") or []) if x in src_id_map],
                root_ids=[cat_id_map[x] for x in (snap.get("root_ids") or []) if x in cat_id_map],
                auto_category_ids=snap_auto_ids,
                summary_lang=snap.get("summary_lang") or "en",
                sample_size=snap.get("sample_size") or 0,
                model=snap.get("model"),
                themes=snap.get("themes") or [],
                created_at=_parse_dt(snap.get("created_at")) or datetime.utcnow(),
            )
        )

    await session.commit()
    return {
        "id": new_inv.id,
        "label": new_inv.label,
        "summary": {
            "sources_inserted": len(src_id_map),
            "categories_resolved": len(cat_id_map),
            "reviews_resolved": len(rev_id_map),
            "auto_categories_inserted": len(ac_id_map),
            "snapshots": len(data.get("theme_snapshots") or []),
        },
    }
