"""Bottom-up success-factor discovery from a /vendors CSV.

Pipeline:

  1. Parse the user-uploaded CSV (same shape as /vendors export).
  2. Filter to type='strength' rows that have non-empty reasons.
  3. Split each row's `reasons` cell on ';' to extract the individual
     causal-mechanism phrases (with their per-mechanism counts when
     the "reason(N)" convention is used).
  4. ONE Claude completion — temperature=0 — clusters the reasons
     into ~5 success-factor categories. The LLM gets every reason
     indexed; it returns categories with arrays of those indexes.
  5. Hydrate the response: map indexes back to full reason+vendor
     +category records so the UI can render evidence per category.

Reasoning behind the design:
  - Index-based grouping keeps the LLM output compact (a few hundred
    integers vs thousands of characters of echoed text).
  - "name in the prompt" is the success-factor LABEL the LLM proposes,
    not a pre-existing taxonomy — by design this is bottom-up.
  - Per-mechanism counts (from "reason(N)") get summed into
    `total_user_count` per category so the UI can rank categories by
    real-world signal volume, not just "number of vendors mentioning
    it."
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import CompetitiveV2Card


# Target category count. The prompt asks for ~5 categories (4-6 acceptable
# range so the LLM has flexibility when the data naturally splits more or
# less than that). Confirmed with the user.
TARGET_CATEGORY_COUNT_MIN = 4
TARGET_CATEGORY_COUNT_MAX = 6

# Soft cap on reasons sent to Claude in a single call. Typical /vendors
# CSV produces ~150 reasons; 400 is the worst-case ceiling that still
# fits in Haiku's context with margin.
MAX_REASONS_IN_PROMPT = 400

# Output budget for the clustering completion. Index-array output is
# small (~100 ints + JSON skeleton + 5 short descriptions) so 4096
# tokens is overkill but defensive.
LLM_MAX_TOKENS = 4096


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    return s.strip()


# Matches the "reason text (N)" convention used in the /vendors reasons
# column. Capture the leading text and the optional trailing count.
_REASON_COUNT_RE = re.compile(r"^(.+?)\s*\((\d+)\)\s*$")


def _parse_reasons_cell(cell: str) -> list[tuple[str, int]]:
    """Split a reasons cell into (text, count) tuples.

    `cell` example: "포인트 추적 시스템(12); 음식 기록 편의성(11); ..."
    Returns: [("포인트 추적 시스템", 12), ("음식 기록 편의성", 11), ...]
    If a piece lacks the "(N)" suffix, count defaults to 1.
    """
    out: list[tuple[str, int]] = []
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
            out.append((text[:300], max(0, count)))
    return out


def _normalize_csv_row(raw: dict) -> Optional[dict]:
    """Minimal coercion — we only need a handful of fields for v2."""
    if not isinstance(raw, dict):
        return None
    vendor = str(raw.get("vendor") or "").strip()
    category = str(raw.get("category") or "").strip()
    if not vendor or not category:
        return None
    return {
        "vendor": vendor[:200],
        "type": str(raw.get("type") or "").strip().lower()[:32] or "strength",
        "category": category[:200],
        "reasons": str(raw.get("reasons") or "").strip()[:2000],
    }


async def _llm_cluster_reasons(
    reason_items: list[dict],
    model: str,
) -> list[dict]:
    """Send the indexed reason list to Claude and parse back the
    {categories: [{name, description, member_reasons: [int]}, ...]}
    response. Returns the parsed categories list (no enrichment yet)."""
    if not reason_items:
        return []
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    from anthropic import AsyncAnthropic

    system = (
        "You analyze user-perceived strengths across competing products "
        "to discover universal SUCCESS FACTORS that drive positive "
        "reviews across the market.\n\n"
        "Each input item is one specific causal mechanism a user gave "
        "for a positive review (e.g. \"바코드 스캔으로 빠른 음식 입력\"). "
        "Different products often express the SAME underlying success "
        "factor in different words — your job is to find those.\n\n"
        f"Cluster the items into approximately {TARGET_CATEGORY_COUNT_MIN}"
        f"–{TARGET_CATEGORY_COUNT_MAX} SUCCESS FACTOR CATEGORIES. For each:\n"
        "  - name: 3–7 word label NAMING the success factor (noun phrase,\n"
        "          in the same language as the input reasons)\n"
        "  - description: ONE sentence on what specifically makes this a\n"
        "                 driver of positive user experience\n"
        "  - member_reasons: array of the [N] indices belonging here\n\n"
        "Rules:\n"
        "  - Every input reason belongs to EXACTLY ONE category (each\n"
        "    index appears in exactly one member_reasons array).\n"
        "  - Avoid vague catch-all categories like \"general satisfaction\"\n"
        "    — categories must be SUBSTANTIVE and DISTINCT.\n"
        "  - Order categories from largest member count to smallest.\n"
        "  - If the data clearly splits into more or fewer than "
        f"{TARGET_CATEGORY_COUNT_MIN}–{TARGET_CATEGORY_COUNT_MAX} natural\n"
        "    clusters, deviate within reason (3 minimum, 8 maximum).\n\n"
        "Output JSON only, no prose, no markdown fences:\n"
        "{\n"
        '  "categories": [\n'
        '    {"name": "...", "description": "...", "member_reasons": [int, ...]}\n'
        "  ]\n"
        "}"
    )
    user = "Reason items:\n" + "\n".join(
        f"[{it['index']}] {it['text']} — (vendor={it['vendor']}, "
        f"category={it['category']}, count={it['count']})"
        for it in reason_items
    )

    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    resp = await client.messages.create(
        model=model,
        max_tokens=LLM_MAX_TOKENS,
        temperature=0.0,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = _strip_fences(
        "".join(getattr(b, "text", "") for b in resp.content)
    )
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"failed to parse LLM JSON: {e}; raw={text[:200]!r}"
        )
    raw_cats = parsed.get("categories") or []
    if not isinstance(raw_cats, list):
        return []
    cleaned: list[dict] = []
    valid_indexes = {it["index"] for it in reason_items}
    seen_globally: set[int] = set()
    for c in raw_cats:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        description = str(c.get("description") or "").strip()
        idxs_raw = c.get("member_reasons") or []
        idxs: list[int] = []
        if isinstance(idxs_raw, list):
            for v in idxs_raw:
                try:
                    i = int(v)
                except (TypeError, ValueError):
                    continue
                # Drop hallucinated indexes + dedup across all categories
                # so the same reason never appears in two clusters.
                if i in valid_indexes and i not in seen_globally:
                    seen_globally.add(i)
                    idxs.append(i)
        cleaned.append({
            "name": name[:120],
            "description": description[:400],
            "member_indexes": idxs,
        })
    return cleaned


async def analyze_csv_v2(
    *,
    rows: list[dict],
    model: Optional[str] = None,
) -> dict:
    """Top-level orchestration. See module docstring for the pipeline.

    Returns dict shape:
      {
        "input_row_count":     int,    # all CSV rows after coercion
        "strength_row_count":  int,    # type=strength subset
        "rows_with_reasons":   int,    # of those, ones with non-empty reasons
        "skipped_empty_rows":  int,    # strength rows dropped (no reasons)
        "reason_count":        int,    # individual mechanisms parsed
        "categories": [
          {
            "name": str,
            "description": str,
            "member_count": int,
            "total_user_count": int,   # sum of mechanism counts in category
            "vendor_count": int,       # distinct vendors represented
            "members": [               # the actual reasons in this category
              {"text", "count", "vendor", "category"}
            ]
          }, ...
        ],
        "model": str,
      }
    """
    # 1. Normalize + strength filter
    cleaned: list[dict] = []
    for r in rows or []:
        n = _normalize_csv_row(r)
        if n:
            cleaned.append(n)
    input_row_count = len(cleaned)
    strengths = [r for r in cleaned if r["type"] == "strength"]
    strength_row_count = len(strengths)

    # 2. Extract individual reason items (with stable index)
    reason_items: list[dict] = []
    rows_with_reasons = 0
    for r in strengths:
        parsed = _parse_reasons_cell(r["reasons"])
        if not parsed:
            continue
        rows_with_reasons += 1
        for text, count in parsed:
            reason_items.append({
                "index": len(reason_items),
                "text": text,
                "count": count,
                "vendor": r["vendor"],
                "category": r["category"],
            })
            if len(reason_items) >= MAX_REASONS_IN_PROMPT:
                break
        if len(reason_items) >= MAX_REASONS_IN_PROMPT:
            break
    skipped_empty_rows = strength_row_count - rows_with_reasons

    if not reason_items:
        return {
            "input_row_count": input_row_count,
            "strength_row_count": strength_row_count,
            "rows_with_reasons": rows_with_reasons,
            "skipped_empty_rows": skipped_empty_rows,
            "reason_count": 0,
            "categories": [],
            "model": (model or settings.ANTHROPIC_MODEL),
            "message": "no_reasons_in_csv",
        }

    # 3. LLM clustering
    chosen_model = (model or settings.ANTHROPIC_MODEL).strip()
    raw_categories = await _llm_cluster_reasons(reason_items, chosen_model)

    # 4. Hydrate categories with member records + counts
    items_by_idx = {it["index"]: it for it in reason_items}
    categories_out: list[dict] = []
    for c in raw_categories:
        members: list[dict] = []
        vendors_seen: set[str] = set()
        total_user_count = 0
        for i in c["member_indexes"]:
            it = items_by_idx.get(i)
            if not it:
                continue
            members.append({
                "text": it["text"],
                "count": it["count"],
                "vendor": it["vendor"],
                "category": it["category"],
            })
            vendors_seen.add(it["vendor"])
            total_user_count += it["count"]
        # Sort members by per-mechanism count desc so the strongest
        # evidence shows up first in the UI expand-list.
        members.sort(key=lambda m: m["count"], reverse=True)
        categories_out.append({
            "name": c["name"],
            "description": c["description"],
            "member_count": len(members),
            "total_user_count": total_user_count,
            "vendor_count": len(vendors_seen),
            "members": members,
        })
    # Rank categories by member count desc (LLM was asked to do this
    # but enforce it server-side too).
    categories_out.sort(key=lambda c: c["member_count"], reverse=True)

    return {
        "input_row_count": input_row_count,
        "strength_row_count": strength_row_count,
        "rows_with_reasons": rows_with_reasons,
        "skipped_empty_rows": skipped_empty_rows,
        "reason_count": len(reason_items),
        "categories": categories_out,
        "model": chosen_model,
    }


# ============================================================================
# CRUD
# ============================================================================


async def list_saved_cards(
    session: AsyncSession, *, include_hidden: bool = False
) -> list[dict]:
    stmt = select(CompetitiveV2Card)
    if not include_hidden:
        stmt = stmt.where(CompetitiveV2Card.hidden.is_(False))
    stmt = stmt.order_by(
        CompetitiveV2Card.display_order.asc(),
        CompetitiveV2Card.updated_at.desc(),
    )
    rows = (await session.execute(stmt)).scalars().all()
    out: list[dict] = []
    for c in rows:
        payload = c.result_payload or {}
        out.append({
            "id": c.id,
            "label": c.label,
            "model_used": c.model_used,
            "hidden": c.hidden,
            "display_order": c.display_order,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            "input_row_count": len(c.input_csv or []),
            "category_count": len(payload.get("categories") or []),
            "reason_count": int(payload.get("reason_count") or 0),
        })
    return out


async def get_saved_card(
    session: AsyncSession, card_id: int
) -> Optional[CompetitiveV2Card]:
    return await session.get(CompetitiveV2Card, card_id)


async def save_card(
    session: AsyncSession,
    *,
    label: Optional[str],
    input_csv: list[dict],
    result_payload: dict,
    model_used: Optional[str],
) -> CompetitiveV2Card:
    if not isinstance(input_csv, list) or not isinstance(result_payload, dict):
        raise ValueError("input_csv must be a list and result_payload a dict")
    max_order = (
        await session.execute(
            select(CompetitiveV2Card.display_order)
            .order_by(CompetitiveV2Card.display_order.desc())
            .limit(1)
        )
    ).scalar_one_or_none() or 0
    card = CompetitiveV2Card(
        label=(label or "성공요인 분석")[:200],
        model_used=(model_used or "")[:100] or None,
        input_csv=input_csv,
        result_payload=result_payload,
        hidden=False,
        display_order=int(max_order) + 1,
    )
    session.add(card)
    await session.commit()
    await session.refresh(card)
    return card


async def update_card_label(
    session: AsyncSession, card_id: int, label: str
) -> Optional[CompetitiveV2Card]:
    card = await session.get(CompetitiveV2Card, card_id)
    if not card:
        return None
    label = (label or "").strip()
    if not label:
        raise ValueError("label cannot be empty")
    card.label = label[:200]
    await session.commit()
    await session.refresh(card)
    return card


async def toggle_card_hidden(
    session: AsyncSession, card_id: int, hidden: bool
) -> Optional[CompetitiveV2Card]:
    card = await session.get(CompetitiveV2Card, card_id)
    if not card:
        return None
    card.hidden = bool(hidden)
    await session.commit()
    await session.refresh(card)
    return card


async def delete_card(session: AsyncSession, card_id: int) -> bool:
    card = await session.get(CompetitiveV2Card, card_id)
    if not card:
        return False
    await session.delete(card)
    await session.commit()
    return True


async def reanalyze_card(
    session: AsyncSession,
    card_id: int,
    *,
    model: Optional[str] = None,
) -> Optional[CompetitiveV2Card]:
    card = await session.get(CompetitiveV2Card, card_id)
    if not card:
        return None
    input_csv = card.input_csv or []
    if not input_csv:
        return card
    new = await analyze_csv_v2(rows=input_csv, model=model)
    card.result_payload = new
    if model:
        card.model_used = model[:100]
    card.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(card)
    return card
