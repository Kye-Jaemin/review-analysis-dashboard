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
    Category,
    CollectionJob,
    Review,
    Sentiment,
    Source,
    SourceType,
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
    "confidence", "summary", "model", "analyzed_at", "status", "error",
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

    payload = {
        "version": EXPORT_VERSION,
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "categories": [_dump(c, CATEGORY_COLS) for c in categories],
        "sources": [_dump(s, SOURCE_COLS) for s in sources],
        "reviews": [_dump(r, REVIEW_COLS) for r in reviews],
        "analyses": [_dump(a, ANALYSIS_COLS) for a in analyses],
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

    for row in data.get("analyses") or []:
        try:
            sent = Sentiment(row["sentiment"]) if row.get("sentiment") else None
        except (ValueError, KeyError):
            sent = None
        try:
            astatus = AnalysisStatus(row["status"]) if row.get("status") else AnalysisStatus.succeeded
        except (ValueError, KeyError):
            astatus = AnalysisStatus.succeeded
        session.add(
            Analysis(
                id=row["id"],
                review_id=row["review_id"],
                category_id=row.get("category_id"),
                sentiment=sent,
                sentiment_score=row.get("sentiment_score"),
                confidence=row.get("confidence"),
                summary=row.get("summary"),
                model=row.get("model"),
                analyzed_at=_parse_dt(row.get("analyzed_at")) or datetime.utcnow(),
                status=astatus,
                error=row.get("error"),
            )
        )
    await session.flush()

    await _reset_pg_sequences(session)
    await session.commit()
    return RedirectResponse(url="/", status_code=303)
