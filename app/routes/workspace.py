"""Workspace snapshot: export / import / reset.

The deployed dashboard has no user accounts; the DB is shared across every
visitor. These endpoints make the "one-investigation-at-a-time, save your
work as a file" workflow viable:

- GET  /workspace/export  → JSON file with every row in every table
- POST /workspace/import  → wipe DB, load rows from uploaded file
- POST /workspace/reset   → wipe DB only
"""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models import (
    Analysis,
    AnalysisJob,
    AnalysisStatus,
    AutoCategory,
    Category,
    CollectionJob,
    Investigation,
    ReviewAutoCategoryLink,
    Review,
    Sentiment,
    Source,
    SourceType,
    ThemeSnapshot,
)

router = APIRouter()

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
AUTO_CATEGORY_COLS = [
    "id", "investigation_id", "name", "description", "review_count", "display_order",
    "language", "translations", "created_at",
]
SNAPSHOT_COLS = [
    "id", "investigation_id", "label", "sentiment", "source_ids", "root_ids",
    "auto_category_ids", "summary_lang", "sample_size", "model", "themes",
    "created_at",
]
INVESTIGATION_COLS = [
    "id", "label", "description", "source_ids", "root_ids",
    "display_order", "created_at", "updated_at",
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
        s = s.rstrip("Z")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _is_postgres() -> bool:
    return settings.database_url.startswith("postgresql")


@router.get("/workspace/export")
async def export_workspace(session: AsyncSession = Depends(get_session)):
    categories = (await session.execute(select(Category))).scalars().all()
    sources = (await session.execute(select(Source))).scalars().all()
    reviews = (await session.execute(select(Review))).scalars().all()
    analyses = (await session.execute(select(Analysis))).scalars().all()
    snapshots = (await session.execute(select(ThemeSnapshot))).scalars().all()
    investigations = (await session.execute(select(Investigation))).scalars().all()
    auto_cats = (await session.execute(select(AutoCategory))).scalars().all()
    links = (
        await session.execute(
            select(
                ReviewAutoCategoryLink.c.review_id,
                ReviewAutoCategoryLink.c.auto_category_id,
            )
        )
    ).all()

    payload = {
        "version": EXPORT_VERSION,
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "categories": [_dump(c, CATEGORY_COLS) for c in categories],
        "sources": [_dump(s, SOURCE_COLS) for s in sources],
        "reviews": [_dump(r, REVIEW_COLS) for r in reviews],
        "analyses": [_dump(a, ANALYSIS_COLS) for a in analyses],
        "theme_snapshots": [_dump(s, SNAPSHOT_COLS) for s in snapshots],
        "investigations": [_dump(i, INVESTIGATION_COLS) for i in investigations],
        "auto_categories": [_dump(a, AUTO_CATEGORY_COLS) for a in auto_cats],
        "review_auto_categories": [
            {"review_id": rid, "auto_category_id": acid} for rid, acid in links
        ],
    }

    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="workspace-{ts}.json"'},
    )


async def _wipe(session: AsyncSession) -> None:
    # FK order: analyses → analysis_jobs → reviews → collection_jobs → sources → categories.
    # ThemeSnapshot and Investigation have no FKs, can be wiped any time.
    # review_auto_categories must go before its two parents.
    await session.execute(delete(ReviewAutoCategoryLink))
    await session.execute(delete(ThemeSnapshot))
    await session.execute(delete(AutoCategory))
    await session.execute(delete(Investigation))
    await session.execute(delete(Analysis))
    await session.execute(delete(AnalysisJob))
    await session.execute(delete(Review))
    await session.execute(delete(CollectionJob))
    await session.execute(delete(Source))
    await session.execute(delete(Category))


async def _reset_pg_sequences(session: AsyncSession) -> None:
    """When we insert rows with explicit ids on Postgres, the SERIAL sequence
    doesn't advance. The next auto-increment would then collide. Bump every
    sequence to MAX(id)+1."""
    if not _is_postgres():
        return
    for table in [
        "analyses", "analysis_jobs", "reviews", "collection_jobs", "sources", "categories",
        "theme_snapshots", "investigations", "auto_categories",
    ]:
        await session.execute(
            text(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'),"
                f" COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)"
            )
        )


@router.post("/workspace/reset")
async def reset_workspace(session: AsyncSession = Depends(get_session)):
    await _wipe(session)
    await _reset_pg_sequences(session)
    await session.commit()
    return RedirectResponse(url="/", status_code=303)


@router.post("/workspace/import")
async def import_workspace(
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
):
    raw = await file.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise HTTPException(400, f"invalid json file: {e}")

    if not isinstance(data, dict) or data.get("version") != EXPORT_VERSION:
        raise HTTPException(
            400, f"unsupported workspace export version: {data.get('version')!r}"
        )

    await _wipe(session)
    await session.flush()

    # Categories — two passes so self-referential parent_id never points at
    # an as-yet-uninserted row.
    cat_rows = data.get("categories") or []
    for row in cat_rows:
        session.add(
            Category(
                id=row["id"],
                parent_id=None,
                name=row.get("name") or "",
                description=row.get("description") or "",
                path=row.get("path") or row.get("name") or "",
            )
        )
    await session.flush()
    for row in cat_rows:
        if row.get("parent_id"):
            cat = await session.get(Category, row["id"])
            if cat is not None:
                cat.parent_id = row["parent_id"]
    await session.flush()

    for row in data.get("sources") or []:
        try:
            stype = SourceType(row["type"])
        except (ValueError, KeyError):
            stype = SourceType.web
        session.add(
            Source(
                id=row["id"],
                type=stype,
                label=row.get("label") or row["type"],
                display_name=row.get("display_name"),
                icon_url=row.get("icon_url"),
                config=row.get("config") or {},
                created_at=_parse_dt(row.get("created_at")) or datetime.utcnow(),
            )
        )
    await session.flush()

    for row in data.get("reviews") or []:
        session.add(
            Review(
                id=row["id"],
                source_id=row["source_id"],
                external_id=row["external_id"],
                author=row.get("author"),
                posted_at=_parse_dt(row.get("posted_at")),
                rating=row.get("rating"),
                text=row.get("text") or "",
                url=row.get("url"),
                raw=row.get("raw") or {},
                collected_at=_parse_dt(row.get("collected_at")) or datetime.utcnow(),
            )
        )
    await session.flush()

    # Investigations + auto_categories land before analyses because
    # Analysis.auto_category_id FKs into them.
    for row in data.get("investigations") or []:
        now = datetime.utcnow()
        existing_inv = await session.get(Investigation, row["id"])
        if existing_inv:
            continue
        session.add(
            Investigation(
                id=row["id"],
                label=row.get("label") or "(unnamed)",
                description=row.get("description"),
                source_ids=row.get("source_ids") or [],
                root_ids=row.get("root_ids") or [],
                display_order=int(row.get("display_order") or 0),
                created_at=_parse_dt(row.get("created_at")) or now,
                updated_at=_parse_dt(row.get("updated_at")) or now,
            )
        )
    await session.flush()
    for row in data.get("auto_categories") or []:
        session.add(
            AutoCategory(
                id=row["id"],
                investigation_id=row["investigation_id"],
                name=row.get("name") or "(unnamed)",
                description=row.get("description"),
                review_count=row.get("review_count") or 0,
                display_order=row.get("display_order") or 0,
                language=row.get("language") or "en",
                translations=row.get("translations") or {},
                created_at=_parse_dt(row.get("created_at")) or datetime.utcnow(),
            )
        )
    await session.flush()

    # Capture (review_id, auto_category_id) pairs hidden inside old v1
    # exports — pre-junction the relation lived as a column on analyses,
    # so we backfill the junction from those values for compat.
    legacy_links: list[tuple[int, int]] = []

    for row in data.get("analyses") or []:
        try:
            sent = Sentiment(row["sentiment"]) if row.get("sentiment") else None
        except (ValueError, KeyError):
            sent = None
        try:
            astatus = AnalysisStatus(row["status"]) if row.get("status") else AnalysisStatus.succeeded
        except (ValueError, KeyError):
            astatus = AnalysisStatus.succeeded
        legacy_ac = row.get("auto_category_id")
        if isinstance(legacy_ac, int):
            legacy_links.append((row["review_id"], legacy_ac))
        session.add(
            Analysis(
                id=row["id"],
                review_id=row["review_id"],
                category_id=row.get("category_id"),
                sentiment=sent,
                sentiment_score=row.get("sentiment_score"),
                user_tier=row.get("user_tier"),
                confidence=row.get("confidence"),
                summary=row.get("summary"),
                model=row.get("model"),
                analyzed_at=_parse_dt(row.get("analyzed_at")) or datetime.utcnow(),
                status=astatus,
                error=row.get("error"),
            )
        )
    await session.flush()

    # New-style junction rows from the export. Old exports won't have this
    # key — that's fine, legacy_links above already covers them.
    junction_rows: list[dict] = []
    seen_pairs: set[tuple[int, int]] = set()
    for row in data.get("review_auto_categories") or []:
        try:
            rid = int(row["review_id"])
            acid = int(row["auto_category_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if (rid, acid) in seen_pairs:
            continue
        seen_pairs.add((rid, acid))
        junction_rows.append({"review_id": rid, "auto_category_id": acid})
    for rid, acid in legacy_links:
        if (rid, acid) in seen_pairs:
            continue
        seen_pairs.add((rid, acid))
        junction_rows.append({"review_id": rid, "auto_category_id": acid})
    if junction_rows:
        await session.execute(ReviewAutoCategoryLink.insert(), junction_rows)
        await session.flush()

    # Investigations have to land BEFORE theme_snapshots because the latter
    # has a FK pointing at them.
    # Investigations were inserted earlier (before analyses) so their
    # FK target exists; nothing more to do here.

    for row in data.get("theme_snapshots") or []:
        session.add(
            ThemeSnapshot(
                id=row["id"],
                investigation_id=row.get("investigation_id"),
                label=row.get("label") or "(unnamed)",
                sentiment=row.get("sentiment") or "neutral",
                source_ids=row.get("source_ids") or [],
                root_ids=row.get("root_ids") or [],
                auto_category_ids=row.get("auto_category_ids") or [],
                summary_lang=row.get("summary_lang") or "en",
                sample_size=row.get("sample_size") or 0,
                model=row.get("model"),
                themes=row.get("themes") or [],
                created_at=_parse_dt(row.get("created_at")) or datetime.utcnow(),
            )
        )
    await session.flush()

    await _reset_pg_sequences(session)
    await session.commit()
    return RedirectResponse(url="/", status_code=303)
