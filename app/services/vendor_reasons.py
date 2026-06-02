"""Per-(vendor, category, sentiment band) "why?" analysis.

When the user clicks a strength or weakness on /vendors, this service:

  1. Resolves the vendor → source_ids and the category → AutoCategory ids
     using the SAME single-vendor, non-hidden filter as list_vendors().
  2. Pulls up to SAMPLE_LIMIT unique review snippets within that scope,
     scoped to the sentiment band the user clicked (positive band for
     strengths = {positive, very_positive}; negative for weaknesses).
  3. Asks Claude to identify 4–6 distinct REASONS the users gave, with
     per-reason counts and 2–3 short example quotes per reason.
  4. Overrides each reason's count with the truthful sample-side count
     (themes.py pattern) so the chart doesn't drift from sample reality.
  5. Optionally persists the result as a VendorReasonCard so the next
     click loads instantly with no Claude call.

Same temperature=0 + deterministic-count pattern we settled on for the
competitive service and the themes mindmap.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    Analysis,
    Review,
    ReviewAutoCategoryLink,
    Sentiment,
    VendorReasonCard,
)
from app.services.vendors import (
    _NEG_BAND,
    _POS_BAND,
    list_vendors,
)


# Up to this many unique reviews are sampled per (vendor, cat, band)
# for the LLM call. Matches the themes mindmap sample size — anything
# larger blows the Haiku context budget for borderline-large categories.
SAMPLE_LIMIT = 100

# Per-reason example cap. Three short quotes are enough to ground the
# reason in real review text without making the modal scroll forever.
EXAMPLES_PER_REASON = 3

# Default Claude budget for the reasons call. The output is small
# (4–6 reasons × short fields) so 2048 is plenty with margin.
LLM_MAX_TOKENS = 2048

# In-memory cache: same (vendor, category, band, lang) → result.
# 30-min TTL so re-clicks within a session don't burn LLM, but Render
# redeploys (which clear the dict) still trigger re-analysis. Persistent
# storage is the VendorReasonCard table for that purpose.
_CACHE_TTL_SECONDS = 30 * 60
_cache: dict[str, tuple[float, dict]] = {}


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    return s.strip()


def _cache_key(vendor_key: str, category_name: str, band: str, lang: str) -> str:
    return f"{vendor_key}|{category_name.lower()}|{band}|{lang}"


def _get_cached(key: str) -> Optional[dict]:
    entry = _cache.get(key)
    if not entry:
        return None
    ts, data = entry
    if time.time() - ts > _CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return data


def _set_cached(key: str, data: dict) -> None:
    _cache[key] = (time.time(), data)


async def _resolve_vendor_category(
    session: AsyncSession,
    vendor_key: str,
    category_name: str,
) -> Optional[dict]:
    """Find the vendor row (display, source_ids) and the AutoCategory ids
    matching the given category name. Returns None if vendor unknown or
    no matching category.
    """
    vendors = await list_vendors(session)
    vendor = next((v for v in vendors if v.get("key") == vendor_key), None)
    if not vendor:
        return None
    target = category_name.strip().lower()
    cat_ids: list[int] = []
    cat_display = category_name
    cat_description: Optional[str] = None
    for s in vendor.get("strengths", []) + vendor.get("weaknesses", []):
        if (s.get("name") or "").strip().lower() == target:
            cat_ids.extend(s.get("cat_ids") or [])
            cat_display = s.get("name") or cat_display
            cat_description = s.get("description") or cat_description
    # Dedup while preserving order.
    seen: set[int] = set()
    deduped_ids: list[int] = []
    for cid in cat_ids:
        if cid not in seen:
            seen.add(cid)
            deduped_ids.append(cid)
    if not deduped_ids:
        return None
    return {
        "vendor": vendor,
        "cat_ids": deduped_ids,
        "category_name": cat_display,
        "category_description": cat_description,
    }


async def _fetch_review_sample(
    session: AsyncSession,
    cat_ids: list[int],
    source_ids: list[int],
    band: str,
    *,
    limit: int = SAMPLE_LIMIT,
) -> list[dict]:
    """Pull up to `limit` unique reviews for (cats, sources, sentiment band).

    Strongest-first ordering: for positive band, most positive
    sentiment_score first; for negative band, most negative first.
    Stable tiebreak by review id.
    """
    sentiments = _POS_BAND if band == "positive" else _NEG_BAND
    sent_values = [Sentiment(s) for s in sentiments]
    desc = band == "positive"
    order_col = (
        Analysis.sentiment_score.desc().nullslast()
        if desc
        else Analysis.sentiment_score.asc().nullsfirst()
    )
    rows = (
        await session.execute(
            select(
                Review.id,
                Review.text,
                Review.rating,
                Analysis.sentiment,
                Analysis.summary,
            )
            .join(Analysis, Analysis.review_id == Review.id)
            .join(
                ReviewAutoCategoryLink,
                ReviewAutoCategoryLink.c.review_id == Review.id,
            )
            .where(ReviewAutoCategoryLink.c.auto_category_id.in_(cat_ids))
            .where(Review.source_id.in_(source_ids))
            .where(Analysis.sentiment.in_(sent_values))
            .order_by(order_col, Review.id.desc())
            # Over-fetch because dedup happens in Python.
            .limit(limit * 2)
        )
    ).all()

    seen: set[int] = set()
    out: list[dict] = []
    for rid, text, rating, sent, summary in rows:
        if rid in seen:
            continue
        seen.add(rid)
        snippet = (summary if summary else (text or "")).strip().replace("\n", " ")
        out.append({
            "id": int(rid),
            "snippet": snippet[:400],
            "rating": int(rating) if rating is not None else None,
            "sentiment": sent.value if hasattr(sent, "value") else str(sent),
        })
        if len(out) >= limit:
            break
    return out


def _band_label(band: str, lang: str) -> str:
    if band == "positive":
        return "positive" if lang != "ko" else "긍정적"
    return "negative" if lang != "ko" else "부정적"


async def _call_claude(
    sample: list[dict],
    vendor_display: str,
    category_name: str,
    category_description: Optional[str],
    band: str,
    summary_lang: str,
    model: str,
) -> list[dict]:
    """Ask Claude for 4–6 reasons. Returns raw reason list (no count
    override yet — caller handles that)."""
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    from anthropic import AsyncAnthropic

    lang_label = {"ko": "Korean", "en": "English"}.get(summary_lang, "English")
    polarity = "POSITIVE (praise / what's working)" if band == "positive" else "NEGATIVE (complaints / what's broken)"
    sub_polarity = "positive" if band == "positive" else "negative"
    description_line = (
        f"Category description: {category_description}\n" if category_description else ""
    )

    system = (
        f"You analyze user reviews to surface the REASONS behind a specific "
        f"sentiment about a specific product category.\n\n"
        f"Vendor: {vendor_display}\n"
        f"Category: {category_name}\n"
        f"{description_line}"
        f"All reviews below share {polarity} sentiment about this category.\n\n"
        f"Identify 4–6 distinct REASONS the users gave for their {sub_polarity} "
        f"feedback. For each reason:\n"
        f"  - reason: short label in {lang_label} (3–8 words, NOUN PHRASE,\n"
        f"            describe WHY users felt this way, not just what feature\n"
        f"            they mentioned)\n"
        f"  - count: number of reviews whose PRIMARY reason is this.\n"
        f"           IMPORTANT: each review contributes to AT MOST ONE reason\n"
        f"           (its strongest); sum of counts MUST be ≤ total reviews.\n"
        f"  - examples: 2–3 SHORT (<60 chars) representative quotes,\n"
        f"              preserve the original review language.\n\n"
        f"Skip vague catch-all reasons (\"users liked it\", \"it works\") —\n"
        f"the reasons must be SUBSTANTIVE and DISTINCT from each other.\n"
        f"Order reasons by count DESC.\n\n"
        f"Output JSON only, no prose, no markdown fences:\n"
        f"{{\n"
        f'  "reasons": [\n'
        f'    {{"reason": "...", "count": N, "examples": ["...", "..."]}}\n'
        f"  ]\n"
        f"}}"
    )
    user_msg = "Reviews:\n" + "\n".join(
        f"[{r['id']}] (★{r['rating'] or '-'}) {r['snippet']}" for r in sample
    )

    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    resp = await client.messages.create(
        model=model,
        max_tokens=LLM_MAX_TOKENS,
        temperature=0.0,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = _strip_fences("".join(getattr(b, "text", "") for b in resp.content))
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"failed to parse LLM JSON: {e}; raw={text[:200]!r}")
    raw = parsed.get("reasons") or []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or "").strip()
        if not reason:
            continue
        try:
            count = int(item.get("count")) if item.get("count") is not None else 0
        except (TypeError, ValueError):
            count = 0
        examples_raw = item.get("examples") or []
        examples: list[str] = []
        if isinstance(examples_raw, list):
            for e in examples_raw[:EXAMPLES_PER_REASON]:
                if e is None:
                    continue
                ex = str(e).strip()
                if ex:
                    examples.append(ex[:120])
        out.append({"reason": reason[:120], "count": max(0, count), "examples": examples})
    return out


def _cap_reason_counts(reasons: list[dict], sample_size: int) -> list[dict]:
    """Re-cap reason counts so their running sum stays ≤ sample_size,
    mirroring the themes.py truth-override step. LLM occasionally
    over-reports; this keeps the chart consistent with the sample.
    """
    running = 0
    for r in reasons:
        c = r.get("count", 0)
        if running + c > sample_size:
            r["count"] = max(0, sample_size - running)
        running += r.get("count", 0)
    return reasons


# ============================================================================
# Public entry points
# ============================================================================


async def extract_reasons(
    session: AsyncSession,
    *,
    vendor_key: str,
    category_name: str,
    band: str,
    summary_lang: str = "en",
    model: Optional[str] = None,
    force: bool = False,
) -> dict:
    """Live extraction (LLM call, with in-memory cache).

    Does NOT touch the DB. The route layer is responsible for checking
    for a persisted VendorReasonCard before calling this — if found and
    not force, it returns that instead.

    Returns:
      {
        "vendor_key", "vendor_display", "category_name",
        "category_description", "band",
        "source_ids", "sample_size",
        "reasons": [{reason, count, examples}, ...],
        "model", "cached": bool,
        "generated_at",
      }
    """
    band = band.lower()
    if band not in ("positive", "negative"):
        raise ValueError("band must be 'positive' or 'negative'")
    vendor_key = (vendor_key or "").strip()
    category_name = (category_name or "").strip()
    if not vendor_key or not category_name:
        raise ValueError("vendor_key and category_name are required")

    ckey = _cache_key(vendor_key, category_name, band, summary_lang)
    if not force:
        hit = _get_cached(ckey)
        if hit:
            return {**hit, "cached": True}

    resolved = await _resolve_vendor_category(session, vendor_key, category_name)
    if not resolved:
        return {
            "vendor_key": vendor_key,
            "vendor_display": vendor_key,
            "category_name": category_name,
            "category_description": None,
            "band": band,
            "source_ids": [],
            "sample_size": 0,
            "reasons": [],
            "error": "vendor or category not found",
            "cached": False,
        }
    vendor = resolved["vendor"]
    source_ids = vendor.get("source_ids") or []

    sample = await _fetch_review_sample(
        session,
        cat_ids=resolved["cat_ids"],
        source_ids=source_ids,
        band=band,
        limit=SAMPLE_LIMIT,
    )
    if not sample:
        result = {
            "vendor_key": vendor_key,
            "vendor_display": vendor.get("display") or vendor_key,
            "category_name": resolved["category_name"],
            "category_description": resolved["category_description"],
            "band": band,
            "source_ids": source_ids,
            "sample_size": 0,
            "reasons": [],
            "model": None,
            "cached": False,
            "generated_at": None,
            "message": "no_reviews_in_band",
        }
        _set_cached(ckey, result)
        return result

    chosen_model = (model or settings.ANTHROPIC_MODEL).strip()
    if chosen_model not in settings.allowed_models:
        chosen_model = settings.ANTHROPIC_MODEL

    raw_reasons = await _call_claude(
        sample,
        vendor_display=vendor.get("display") or vendor_key,
        category_name=resolved["category_name"],
        category_description=resolved["category_description"],
        band=band,
        summary_lang=summary_lang,
        model=chosen_model,
    )
    # Cap counts against actual sample size (truth override).
    capped = _cap_reason_counts(raw_reasons, len(sample))

    result = {
        "vendor_key": vendor_key,
        "vendor_display": vendor.get("display") or vendor_key,
        "category_name": resolved["category_name"],
        "category_description": resolved["category_description"],
        "band": band,
        "source_ids": source_ids,
        "sample_size": len(sample),
        "reasons": capped,
        "model": chosen_model,
        "cached": False,
        "generated_at": time.time(),
    }
    _set_cached(ckey, result)
    return result


# ============================================================================
# Saved-card CRUD (mirrors competitive_card pattern)
# ============================================================================


async def list_saved_cards(
    session: AsyncSession, *, include_hidden: bool = False
) -> list[dict]:
    stmt = select(VendorReasonCard)
    if not include_hidden:
        stmt = stmt.where(VendorReasonCard.hidden.is_(False))
    stmt = stmt.order_by(VendorReasonCard.updated_at.desc())
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": c.id,
            "vendor_key": c.vendor_key,
            "vendor_display": c.vendor_display,
            "category_name": c.category_name,
            "band": c.band,
            "label": c.label,
            "sample_size": c.sample_size,
            "reasons_count": len(c.reasons or []),
            "hidden": c.hidden,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        }
        for c in rows
    ]


async def find_card_lookup(
    session: AsyncSession,
) -> dict[tuple[str, str, str], int]:
    """Return {(vendor_key, category_name_lower, band): card_id} for every
    visible card. Used by the /vendors page to render 📌 indicators on
    strengths/weaknesses the user has saved."""
    rows = (
        await session.execute(
            select(
                VendorReasonCard.id,
                VendorReasonCard.vendor_key,
                VendorReasonCard.category_name,
                VendorReasonCard.band,
            ).where(VendorReasonCard.hidden.is_(False))
        )
    ).all()
    return {
        (vk, (cn or "").strip().lower(), b): cid
        for cid, vk, cn, b in rows
    }


async def get_card(session: AsyncSession, card_id: int) -> Optional[VendorReasonCard]:
    return await session.get(VendorReasonCard, card_id)


async def save_card(
    session: AsyncSession,
    *,
    result: dict,
    label: Optional[str] = None,
) -> VendorReasonCard:
    """Persist a fresh extract_reasons() output as a new card row."""
    vendor_key = (result.get("vendor_key") or "").strip()
    category_name = (result.get("category_name") or "").strip()
    band = (result.get("band") or "").strip()
    if not vendor_key or not category_name or band not in ("positive", "negative"):
        raise ValueError("result is missing vendor_key / category_name / band")
    card = VendorReasonCard(
        vendor_key=vendor_key[:100],
        vendor_display=(result.get("vendor_display") or vendor_key)[:200],
        category_name=category_name[:200],
        band=band,
        label=(label or category_name)[:200],
        model_used=(result.get("model") or "")[:100] or None,
        sample_size=int(result.get("sample_size") or 0),
        source_ids_snapshot=list(result.get("source_ids") or []),
        reasons=result.get("reasons") or [],
        hidden=False,
    )
    session.add(card)
    await session.commit()
    await session.refresh(card)
    return card


async def update_card_label(
    session: AsyncSession, card_id: int, label: str
) -> Optional[VendorReasonCard]:
    card = await session.get(VendorReasonCard, card_id)
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
) -> Optional[VendorReasonCard]:
    card = await session.get(VendorReasonCard, card_id)
    if not card:
        return None
    card.hidden = bool(hidden)
    await session.commit()
    await session.refresh(card)
    return card


async def delete_card(session: AsyncSession, card_id: int) -> bool:
    card = await session.get(VendorReasonCard, card_id)
    if not card:
        return False
    await session.delete(card)
    await session.commit()
    return True


async def reanalyze_card(
    session: AsyncSession,
    card_id: int,
    *,
    summary_lang: str = "en",
    model: Optional[str] = None,
) -> Optional[VendorReasonCard]:
    """Re-run LLM analysis using the card's saved (vendor_key, category,
    band) and overwrite this row's reasons + sample_size + source snapshot.
    """
    card = await session.get(VendorReasonCard, card_id)
    if not card:
        return None
    fresh = await extract_reasons(
        session,
        vendor_key=card.vendor_key,
        category_name=card.category_name,
        band=card.band,
        summary_lang=summary_lang,
        model=model,
        force=True,
    )
    card.reasons = fresh.get("reasons") or []
    card.sample_size = int(fresh.get("sample_size") or 0)
    card.source_ids_snapshot = list(fresh.get("source_ids") or [])
    if fresh.get("model"):
        card.model_used = fresh["model"][:100]
    card.vendor_display = (fresh.get("vendor_display") or card.vendor_display)[:200]
    card.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(card)
    return card
