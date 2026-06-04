"""Competitive-v3 — read-only "what did each vendor's users say?" view.

The user uploads a CSV or Excel file exported from /vendors and the
page shows, per vendor, every strength category + its `reasons` list
(causal mechanisms with counts). Clicking a reason opens a popup with
the actual review snippets that fed that reason in the original
/vendors per-strength analysis.

No DB persistence for v3 itself — the page is a viewer. The popup
lookup chases the VendorReasonCard that produced the row's reasons
(matched by vendor_key + lowercased category_name + band='positive')
and uses its stored review_ids to fetch real review text.

CSV and Excel uploads both supported; the parser dispatches by file
extension/content-type.
"""
from __future__ import annotations

import csv as _csv
import io
import re
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Analysis, Review, VendorReasonCard
from app.services.vendors import list_vendors, _vendor_key


# Same convention used by the v2 reasons parser — "reason text (N)".
_REASON_COUNT_RE = re.compile(r"^(.+?)\s*\((\d+)\)\s*$")


def parse_reasons_cell(cell: str) -> list[dict]:
    """Split a reasons cell ("이유A(12); 이유B(8); …") into structured
    entries. Returns [{text, count}, ...]."""
    out: list[dict] = []
    for piece in (cell or "").split(";"):
        p = piece.strip()
        if not p:
            continue
        m = _REASON_COUNT_RE.match(p)
        if m:
            text = m.group(1).strip()
            try:
                count = int(m.group(2))
            except ValueError:
                count = 1
        else:
            text = p
            count = 1
        if text:
            out.append({"text": text[:300], "count": max(0, count)})
    return out


def _merge_split_reason_rows(rows: list[dict]) -> list[dict]:
    """Fold rows from the row-per-reason XLSX layout back into a single
    row per (vendor, type, category).

    The new XLSX format emits one row per reason — vendor/category/pct
    cells repeat, the trailing column is a singular `reason` cell with
    one entry. This function detects that layout (presence of `reason`
    or absence of `reasons`) and concatenates the entries into a single
    `reasons` cell joined by "; ", so build_view sees the canonical
    shape regardless of which export format the user uploaded.

    Legacy CSV / one-row-per-category XLSX uploads pass through
    unchanged because each (vendor, type, category) key appears only
    once and already has its `reasons` cell populated.
    """
    merged: dict[tuple[str, str, str], dict] = {}
    order: list[tuple[str, str, str]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        key = (
            (r.get("vendor") or "").strip(),
            (r.get("type") or "").strip(),
            (r.get("category") or "").strip(),
        )
        if not any(key):
            # Junk row — let downstream skip it
            order.append(key)
            merged[key] = r
            continue
        single = (r.get("reason") or "").strip()
        if key not in merged:
            merged[key] = dict(r)
            order.append(key)
            if "reasons" not in merged[key]:
                merged[key]["reasons"] = ""
        if single:
            existing = (merged[key].get("reasons") or "").strip()
            merged[key]["reasons"] = (
                f"{existing}; {single}" if existing else single
            )
    return [merged[k] for k in order]


def _score_sheet(headers: list[str]) -> int:
    """Score how likely a sheet is the 'vendor_analysis' export.
    Higher = better match. Used to pick the right sheet when an
    uploaded workbook has multiple sheets (e.g. summary +
    vendor_analysis from the user's categorized file)."""
    low = [str(h or "").strip().lower() for h in headers]
    raw = [str(h or "").strip() for h in headers]
    score = 0
    if "vendor" in low: score += 3
    if "category" in low: score += 3
    if "type" in low: score += 2
    if "reason" in low or "reasons" in low: score += 2
    if "count" in low: score += 1
    # The categorized-mode 카테고리 column is a strong signal that this
    # is the right sheet (vs. the summary pivot, which uses other
    # Korean labels but rarely the exact "카테고리" string as a header).
    if "카테고리" in raw and "vendor" in low: score += 2
    return score


def _pick_main_sheet(wb):
    """Choose the sheet that looks like vendor_analysis. openpyxl's
    `wb.active` reflects whichever sheet was selected at save time,
    which for the user's file is 'summary' — not what we want. Scan
    every sheet, score by header keywords, pick the highest."""
    best = None
    best_score = -1
    for name in wb.sheetnames:
        ws = wb[name]
        headers_row = next(ws.iter_rows(values_only=True, max_row=1), None) or ()
        score = _score_sheet(list(headers_row))
        if score > best_score:
            best_score = score
            best = ws
    return best if best else wb.active


def parse_uploaded_file(filename: str, content: bytes) -> list[dict]:
    """Read a /vendors-export-shaped CSV or XLSX into a list of dict
    rows. Dispatch by extension; both shapes have the same column names
    so downstream code doesn't care which path produced the data.

    Returns RAW per-reason rows (no row-merge). Caller decides:
      - build_view() merges per (vendor, type, category) for the
        per-vendor display.
      - build_categorized_view() keeps rows separate and groups by
        the top-level 카테고리 column instead.

    Returns rows as plain dicts with string values (caller can coerce).
    """
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = _pick_main_sheet(wb)
        rows_iter = ws.iter_rows(values_only=True)
        headers_row = next(rows_iter, None)
        if not headers_row:
            return []
        headers = [str(h or "").strip() for h in headers_row]
        out: list[dict] = []
        for r in rows_iter:
            if not r:
                continue
            entry = {}
            for i, h in enumerate(headers):
                if i >= len(r):
                    continue
                v = r[i]
                entry[h] = "" if v is None else str(v)
            if entry:
                out.append(entry)
        return out
    # default: CSV (handles BOM via utf-8-sig)
    text = content.decode("utf-8-sig", errors="replace")
    reader = _csv.DictReader(io.StringIO(text))
    return [dict(r) for r in reader]


def has_top_category(rows: list[dict]) -> bool:
    """True when any uploaded row carries a non-empty 카테고리 column —
    the signal that the file came from the categorized-export workflow
    and should be rendered with the per-category aggregation view."""
    for r in rows or []:
        if isinstance(r, dict) and (r.get("카테고리") or "").strip():
            return True
    return False


def _row_band_from_type(t: str) -> str:
    t = (t or "").strip().lower()
    if t == "weakness":
        return "negative"
    return "positive"


async def build_view(
    session: AsyncSession,
    raw_rows: list[dict],
) -> dict:
    """Turn parsed rows into the structure the template renders.

    Accepts raw per-reason rows; calls _merge_split_reason_rows first
    so it sees the canonical one-row-per-(vendor,type,category) shape
    with a semicolon-joined `reasons` cell.

    Pipeline:
      1. Merge per-reason rows back into per-category rows.
      2. Normalize each row, filter to those with reasons.
      3. Look up vendor logo + canonical key by matching the row's
         vendor display string against list_vendors() output.
      4. Group rows by canonical vendor key, then by (category, band)
         within that vendor. Each group keeps its parsed reasons +
         the originating row metadata for the popup lookup later.

    Returns:
      {
        "input_row_count":     int,
        "rows_with_reasons":   int,
        "vendors_with_reasons": int,
        "skipped_no_reasons":  int,
        "vendors": [
          {
            "key":          str,    # canonical vendor key
            "display":      str,    # display name
            "icon_url":     str|None,
            "categories": [
              {
                "category": str,
                "band":     "positive"|"negative",
                "pct":      float,
                "count":    int,
                "reasons":  [{"text", "count"}, ...],
              }, ...
            ]
          }, ...
        ]
      }
    """
    # Build a display-name → (canonical_key, icon_url) lookup so we can
    # enrich CSV rows with logos. Match case-insensitively because the
    # CSV display string came from the same list_vendors() output that
    # we're now reading back here.
    vendors_db = await list_vendors(session)
    by_display: dict[str, dict] = {}
    by_key: dict[str, dict] = {}
    for v in vendors_db:
        rec = {"key": v.get("key"), "icon_url": v.get("icon_url"), "display": v.get("display")}
        if v.get("display"):
            by_display[str(v["display"]).strip().lower()] = rec
        if v.get("key"):
            by_key[str(v["key"]).strip().lower()] = rec

    # parse_uploaded_file now returns RAW rows so build_categorized_view
    # can group by reason. The per-vendor view here still wants merged
    # rows (one per vendor,type,category with joined `reasons`), so
    # collapse here.
    raw_rows = _merge_split_reason_rows(raw_rows or [])

    grouped: dict[str, dict] = {}
    rows_with_reasons = 0
    skipped_no_reasons = 0
    input_row_count = 0

    for raw in raw_rows or []:
        if not isinstance(raw, dict):
            continue
        input_row_count += 1
        vendor_display = str(raw.get("vendor") or "").strip()
        category = str(raw.get("category") or "").strip()
        if not vendor_display or not category:
            continue
        reasons_cell = str(raw.get("reasons") or "").strip()
        reasons = parse_reasons_cell(reasons_cell)
        if not reasons:
            skipped_no_reasons += 1
            continue
        rows_with_reasons += 1
        # Resolve canonical vendor key + logo. Try display match first
        # (covers exact /vendors export rows), then fall back to a key
        # match in case the CSV came from a workspace that renamed
        # things.
        rec = by_display.get(vendor_display.lower()) or by_key.get(vendor_display.lower())
        if rec:
            vendor_key = rec["key"]
            icon_url = rec.get("icon_url")
            display = rec.get("display") or vendor_display
        else:
            # Fallback: derive a stable-ish key from the display name
            # using the same stemmer the live page uses; logos remain
            # absent but the user still sees the data.
            vendor_key = _vendor_key(vendor_display, vendor_display)
            icon_url = None
            display = vendor_display

        try:
            pct = float(raw.get("pct") or 0)
        except (TypeError, ValueError):
            pct = 0.0
        try:
            count = int(raw.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        band = _row_band_from_type(str(raw.get("type") or ""))

        vendor_node = grouped.setdefault(vendor_key, {
            "key": vendor_key,
            "display": display,
            "icon_url": icon_url,
            "categories": [],
        })
        # Prefer the longest display we encounter (mirrors list_vendors
        # behavior so users see "Weight Watchers Program" rather than a
        # store-stub).
        if len(display) > len(vendor_node["display"] or ""):
            vendor_node["display"] = display
        if icon_url and not vendor_node["icon_url"]:
            vendor_node["icon_url"] = icon_url
        vendor_node["categories"].append({
            "category": category,
            "band": band,
            "pct": pct,
            "count": count,
            "reasons": reasons,
        })

    # Stable display order: vendors by display name; within a vendor,
    # categories by band (positive first) then by pct desc.
    vendors_out = sorted(
        grouped.values(),
        key=lambda v: (v["display"] or "").lower(),
    )
    for v in vendors_out:
        v["categories"].sort(
            key=lambda c: (
                0 if c["band"] == "positive" else 1,
                -float(c["pct"] or 0),
            )
        )

    return {
        "input_row_count": input_row_count,
        "rows_with_reasons": rows_with_reasons,
        "vendors_with_reasons": len(vendors_out),
        "skipped_no_reasons": skipped_no_reasons,
        "vendors": vendors_out,
    }


# ----------------------------------------------------------------------------
# Categorized view — group reasons by top-level 카테고리 column
# ----------------------------------------------------------------------------


async def build_categorized_view(
    session: AsyncSession,
    raw_rows: list[dict],
) -> dict:
    """When the uploaded XLSX has a per-reason 카테고리 column (top-level
    grouping decided by an upstream classifier), pivot the data around
    those top categories instead of around vendors.

    Each input row is one reason. We aggregate:
      - per top category: total review_count + reason_count + list of
        vendors contributing reasons to this category
      - vendor × category matrix: how many reviews each vendor
        contributed to each top category (matches the Summary sheet's
        pivot table)

    Returns:
      {
        "categorized":   True,                # disambiguates from build_view
        "totals":        {reviews, reasons, vendors, categories},
        "categories": [
          {
            "name":          str,
            "review_count":  int,             # sum across all reasons
            "pct":           float,           # of total reviews
            "reason_count":  int,
            "vendors": [
              {key, display, icon_url, review_count, reasons: [
                {text, count, vendor_category, vendor_pct, band}
              ]}, ...
            ]
          }, ...
        ],
        "matrix": {
          "categories":   [str, ...],
          "vendors":      [{key, display, icon_url}, ...],
          "cells":        [[int, ...], ...],  # row = category, col = vendor
          "row_totals":   [int, ...],
          "col_totals":   [int, ...],
          "grand_total":  int,
        }
      }
    """
    # Vendor logo / canonical key lookup — same approach as build_view.
    vendors_db = await list_vendors(session)
    by_display: dict[str, dict] = {}
    by_key: dict[str, dict] = {}
    for v in vendors_db:
        rec = {
            "key": v.get("key"),
            "icon_url": v.get("icon_url"),
            "display": v.get("display"),
        }
        if v.get("display"):
            by_display[str(v["display"]).strip().lower()] = rec
        if v.get("key"):
            by_key[str(v["key"]).strip().lower()] = rec

    # Working dictionaries — we'll sort + materialize at the end.
    by_top: dict[str, dict] = {}
    matrix: dict[tuple[str, str], int] = {}      # (top_cat_name, vendor_key) → reviews
    vendor_meta: dict[str, dict] = {}            # vendor_key → {key, display, icon_url}
    total_reviews = 0
    reason_rows = 0

    for raw in raw_rows or []:
        if not isinstance(raw, dict):
            continue
        top = (raw.get("카테고리") or "").strip()
        if not top:
            continue
        vendor_display_raw = str(raw.get("vendor") or "").strip()
        vendor_category = str(raw.get("category") or "").strip()
        reason_text = str(raw.get("reason") or "").strip()
        if not vendor_display_raw or not vendor_category or not reason_text:
            continue

        # Per-reason review count: prefer the explicit numeric column;
        # fall back to parsing the "(N)" suffix on the reason cell when
        # the numeric column is missing or 0 (legacy CSVs).
        try:
            review_count = int(float(raw.get("review_count") or 0))
        except (TypeError, ValueError):
            review_count = 0
        m = _REASON_COUNT_RE.match(reason_text)
        if m:
            if not review_count:
                try:
                    review_count = int(m.group(2))
                except ValueError:
                    pass
            reason_text = m.group(1).strip()

        # Resolve canonical vendor + logo.
        rec = (
            by_display.get(vendor_display_raw.lower())
            or by_key.get(vendor_display_raw.lower())
        )
        if rec:
            vendor_key = rec["key"]
            icon_url = rec.get("icon_url")
            display = rec.get("display") or vendor_display_raw
        else:
            vendor_key = _vendor_key(vendor_display_raw, vendor_display_raw)
            icon_url = None
            display = vendor_display_raw

        band = (
            "negative"
            if str(raw.get("type") or "").strip().lower() == "weakness"
            else "positive"
        )
        try:
            vendor_pct = float(raw.get("pct") or 0)
        except (TypeError, ValueError):
            vendor_pct = 0.0

        # Build the nested structure.
        top_node = by_top.setdefault(top, {
            "name": top,
            "vendors": {},
            "review_count": 0,
            "reason_count": 0,
        })
        v_node = top_node["vendors"].setdefault(vendor_key, {
            "key": vendor_key,
            "display": display,
            "icon_url": icon_url,
            "review_count": 0,
            "reasons": [],
        })
        v_node["reasons"].append({
            "text": reason_text,
            "count": review_count,
            "vendor_category": vendor_category,
            "vendor_pct": vendor_pct,
            "band": band,
        })
        v_node["review_count"] += review_count
        # Prefer the longest display we've seen for this vendor (mirrors
        # build_view behaviour for cross-store mergers).
        if len(display) > len(v_node["display"] or ""):
            v_node["display"] = display
        if icon_url and not v_node.get("icon_url"):
            v_node["icon_url"] = icon_url

        top_node["review_count"] += review_count
        top_node["reason_count"] += 1
        total_reviews += review_count
        reason_rows += 1

        matrix[(top, vendor_key)] = matrix.get((top, vendor_key), 0) + review_count
        prev = vendor_meta.get(vendor_key)
        if not prev or len(display) > len(prev.get("display") or ""):
            vendor_meta[vendor_key] = {
                "key": vendor_key,
                "display": display,
                "icon_url": icon_url or (prev or {}).get("icon_url"),
            }

    # Sort: top categories by review_count desc; within each, vendors
    # by review_count desc; within each vendor, reasons by count desc.
    categories_sorted: list[dict] = []
    for top_node in sorted(by_top.values(), key=lambda c: -c["review_count"]):
        top_node["pct"] = (
            (top_node["review_count"] / total_reviews * 100)
            if total_reviews else 0.0
        )
        vendors_sorted = sorted(
            top_node["vendors"].values(),
            key=lambda v: -v["review_count"],
        )
        for v in vendors_sorted:
            v["reasons"].sort(key=lambda r: -r["count"])
        top_node["vendors"] = vendors_sorted
        categories_sorted.append(top_node)

    # Matrix: vendors sorted by total contribution desc so the most
    # active vendors land near the left edge.
    vendor_totals = {
        vk: sum(matrix.get((top["name"], vk), 0) for top in categories_sorted)
        for vk in vendor_meta
    }
    sorted_vendor_keys = sorted(
        vendor_meta.keys(),
        key=lambda vk: -vendor_totals.get(vk, 0),
    )
    sorted_top_names = [c["name"] for c in categories_sorted]

    cells: list[list[int]] = []
    for top in sorted_top_names:
        cells.append([matrix.get((top, vk), 0) for vk in sorted_vendor_keys])
    row_totals = [sum(row) for row in cells]
    col_totals = [
        sum(cells[r][c] for r in range(len(cells)))
        for c in range(len(sorted_vendor_keys))
    ]
    grand_total = sum(row_totals)
    # Precompute max cell value for the heatmap intensity — Jinja's
    # nested-loop {% set %} can't reach outer scope so this can't be
    # done in the template.
    max_cell = 0
    for row in cells:
        for n in row:
            if n > max_cell:
                max_cell = n
    if max_cell == 0:
        max_cell = 1

    return {
        "categorized": True,
        "totals": {
            "reviews": total_reviews,
            "reasons": reason_rows,
            "vendors": len(vendor_meta),
            "categories": len(categories_sorted),
        },
        "categories": categories_sorted,
        "matrix": {
            "categories": sorted_top_names,
            "vendors": [vendor_meta[vk] for vk in sorted_vendor_keys],
            "cells": cells,
            "row_totals": row_totals,
            "col_totals": col_totals,
            "grand_total": grand_total,
            "max_cell": max_cell,
        },
    }


# ----------------------------------------------------------------------------
# Click-a-reason → fetch real reviews
# ----------------------------------------------------------------------------


def _reason_norm(s: str) -> str:
    """Normalize a reason text for fuzzy equality. Strips whitespace +
    lowercases, since CSV reasons cell joins them as 'text(N)' while
    the saved card keeps just the text — the count suffix is stripped
    upstream but normalization is defensive against odd whitespace."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


async def reason_reviews(
    session: AsyncSession,
    *,
    vendor_key: str,
    category_name: str,
    band: str,
    reason_text: str,
) -> dict:
    """Find the VendorReasonCard matching (vendor_key, category_name,
    band) and look up the saved reason whose text matches reason_text.
    Return the hydrated review records from that reason's review_ids.

    Returns:
      {
        "vendor_key": str, "category_name": str, "band": str,
        "reason_text": str, "matched_reason": bool,
        "card_id": int|None,
        "reviews": [{id, text, rating, sentiment, posted_at, ...}, ...],
        "error": str|None,
      }
    """
    band = (band or "positive").strip().lower()
    if band not in ("positive", "negative"):
        band = "positive"
    target = _reason_norm(reason_text)
    out: dict[str, Any] = {
        "vendor_key": vendor_key,
        "category_name": category_name,
        "band": band,
        "reason_text": reason_text,
        "matched_reason": False,
        "card_id": None,
        "reviews": [],
        "error": None,
    }
    cat_lower = (category_name or "").strip().lower()

    # Find candidate saved cards. Match (vendor_key, category_name, band).
    cards = (
        await session.execute(
            select(VendorReasonCard)
            .where(VendorReasonCard.vendor_key == vendor_key)
            .where(VendorReasonCard.band == band)
            .where(VendorReasonCard.hidden.is_(False))
        )
    ).scalars().all()
    matched_card = None
    for c in cards:
        if (c.category_name or "").strip().lower() == cat_lower:
            matched_card = c
            break
    if not matched_card:
        out["error"] = "no_saved_card_for_this_strength"
        return out
    out["card_id"] = matched_card.id

    # Find the matching reason inside the card's reasons payload.
    payload = matched_card.reasons
    if isinstance(payload, dict):
        reason_list = payload.get("reasons") or []
    elif isinstance(payload, list):
        reason_list = payload
    else:
        reason_list = []
    matched_entry: Optional[dict] = None
    for r in reason_list:
        if not isinstance(r, dict):
            continue
        if _reason_norm(r.get("reason") or "") == target:
            matched_entry = r
            break
    if not matched_entry:
        out["error"] = "no_matching_reason_in_saved_card"
        return out
    out["matched_reason"] = True

    review_ids = matched_entry.get("review_ids") or []
    if not isinstance(review_ids, list):
        review_ids = []
    review_ids = [int(x) for x in review_ids if isinstance(x, (int, str)) and str(x).isdigit()]
    if not review_ids:
        out["error"] = "no_review_ids_on_saved_reason"
        return out

    # Hydrate from DB.
    if len(review_ids) > 500:
        review_ids = review_ids[:500]
    rows = (
        await session.execute(
            select(
                Review.id, Review.text, Review.rating, Review.author,
                Review.posted_at, Review.collected_at,
                Analysis.sentiment, Analysis.sentiment_score,
            )
            .join(Analysis, Analysis.review_id == Review.id, isouter=True)
            .where(Review.id.in_(review_ids))
        )
    ).all()
    by_id: dict[int, dict] = {}
    for rid, text, rating, author, posted, collected, sent, sscore in rows:
        s_key = sent.value if hasattr(sent, "value") else (str(sent) if sent else None)
        by_id[int(rid)] = {
            "id": int(rid),
            "text": text or "",
            "rating": int(rating) if rating is not None else None,
            "author": author or "",
            "posted_at": posted.isoformat() if posted else None,
            "collected_at": collected.isoformat() if collected else None,
            "sentiment": s_key,
            "sentiment_score": int(sscore) if sscore is not None else None,
        }
    out["reviews"] = [by_id[i] for i in review_ids if i in by_id]
    return out
