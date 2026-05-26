from __future__ import annotations

import csv
import io
import json
from typing import Iterable

from app.models import Review

COLUMNS = [
    "id", "source", "external_id", "author", "posted_at", "rating",
    "text", "category_path", "sentiment", "sentiment_score",
    "confidence", "summary", "collected_at", "analyzed_at",
]


def _row(r: Review) -> dict:
    a = r.analysis
    return {
        "id": r.id,
        "source": r.source.label if r.source else "",
        "external_id": r.external_id,
        "author": r.author or "",
        "posted_at": r.posted_at.isoformat() if r.posted_at else "",
        "rating": r.rating,
        "text": r.text,
        "category_path": (a.category.path if a and a.category else ""),
        "sentiment": (a.sentiment.value if a and a.sentiment else ""),
        "sentiment_score": a.sentiment_score if a else None,
        "confidence": a.confidence if a else None,
        "summary": (a.summary if a and a.summary else ""),
        "collected_at": r.collected_at.isoformat() if r.collected_at else "",
        "analyzed_at": a.analyzed_at.isoformat() if a and a.analyzed_at else "",
    }


def to_csv(rows: Iterable[Review]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS)
    writer.writeheader()
    for r in rows:
        writer.writerow(_row(r))
    return buf.getvalue().encode("utf-8-sig")


def to_json(rows: Iterable[Review]) -> bytes:
    return json.dumps([_row(r) for r in rows], ensure_ascii=False, indent=2, default=str).encode("utf-8")


def to_xlsx(rows: Iterable[Review]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Reviews"
    ws.append(COLUMNS)
    for r in rows:
        d = _row(r)
        ws.append([d[c] for c in COLUMNS])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
