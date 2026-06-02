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


# Effectively the FULL set of reviews for this (vendor, cat, band)
# triple. Positive-only or negative-only slices for a single category
# rarely exceed a few hundred; 2000 is a hard cap that protects against
# pathological cases without truncating real workloads. Combined with
# SNIPPET_CHARS below the worst case is ~2000 × 250 chars ≈ 500K chars,
# well under Haiku 4.5's 200K-token context window.
#
# (Previous value was 100, which was a "themes mindmap" carryover — the
# user explicitly asked for full coverage since positive-only categories
# are usually small enough that capping made every chart look identical
# regardless of the underlying corpus size.)
SAMPLE_LIMIT = 2000

# Per-review character cap for the snippet sent to the LLM. Shorter than
# the themes-mindmap 400-char cap because we're now sending many more
# reviews per call.
SNIPPET_CHARS = 250

# Per-reason example cap. Three short quotes are enough to ground the
# reason in real review text without making the modal scroll forever.
EXAMPLES_PER_REASON = 3

# Default Claude budget for the reasons call. Bumped from 2048 to 4096
# so the LLM has headroom for the longer example quotes that surface
# when more reviews are available (we now pass effectively all reviews,
# not a 100-row sample). Output is still small (4–6 reasons × short
# fields) so this is purely defensive.
LLM_MAX_TOKENS = 4096

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
            "snippet": snippet[:SNIPPET_CHARS],
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
) -> dict:
    """Ask Claude for 4–6 causal reasons + a separate simple-response
    bucket. Returns
      {"reasons": [{reason, count, examples}, ...],
       "simple_responses": {count: int, examples: [str, ...]}}
    (no count override yet — caller handles that)."""
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    from anthropic import AsyncAnthropic

    lang_label = {"ko": "Korean", "en": "English"}.get(summary_lang, "English")
    polarity = "POSITIVE (praise / what's working)" if band == "positive" else "NEGATIVE (complaints / what's broken)"
    sub_polarity = "positive" if band == "positive" else "negative"
    description_line = (
        f"Category description: {category_description}\n" if category_description else ""
    )
    # Polarity-aware examples for the "outcome vs cause" rule. For positive
    # reviews simple = "X 했어요" / "lost N pounds"; for negative simple =
    # "별로예요" / "doesn't work". The LLM gets concrete pattern matches
    # for both languages so it doesn't have to guess.
    if band == "positive":
        outcome_examples = (
            '       BAD  (outcome statement):  "100파운드 감량 성공", '
            '"lost 20kg in a year", "체중이 빠졌어요", "great results"\n'
            '       GOOD (causal mechanism):  "포인트 시스템이 칼로리를 의식하게 만들어줌", '
            '"AI coach kept me accountable", "커뮤니티 응원이 동기 부여"'
        )
    else:
        outcome_examples = (
            '       BAD  (outcome statement):  "별로예요", "doesn\'t work for me", '
            '"실망", "useless"\n'
            '       GOOD (causal mechanism):  "동기화 오류로 데이터 손실", '
            '"광고가 너무 잦아 사용 흐름 끊김", "AI 인식이 부정확해 매번 수정 필요"'
        )

    system = (
        f"You analyze user reviews to surface the REASONS behind a specific "
        f"sentiment about a specific product category.\n\n"
        f"Vendor: {vendor_display}\n"
        f"Category: {category_name}\n"
        f"{description_line}"
        f"All reviews below share {polarity} sentiment about this category.\n\n"
        f"CRITICAL — CAUSE vs OUTCOME:\n"
        f"  A review that only STATES the outcome (\"I lost weight!\", "
        f"\"앱이 별로\") is NOT a reason. A causal reason explains the\n"
        f"  MECHANISM — what specifically about the product produced that\n"
        f"  outcome, or what specifically went wrong.\n"
        f"  Examples (study both lines):\n"
        f"{outcome_examples}\n\n"
        f"  Outcome-only reviews go into the SEPARATE `simple_responses`\n"
        f"  bucket, NOT into the main reasons list. The user explicitly\n"
        f"  wants to see causal mechanisms, with outcome-only counts\n"
        f"  surfaced separately as a meta signal.\n\n"
        f"For the main reasons:\n"
        f"  Identify 4–6 distinct CAUSAL REASONS the users gave for their\n"
        f"  {sub_polarity} feedback. For each reason:\n"
        f"  - reason: short label in {lang_label} (3–8 words, NOUN PHRASE,\n"
        f"            describe the CAUSAL MECHANISM, not the outcome)\n"
        f"  - count: number of reviews whose PRIMARY reason is this.\n"
        f"           IMPORTANT: each review contributes to AT MOST ONE\n"
        f"           reason (its strongest), and outcome-only reviews\n"
        f"           contribute to simple_responses INSTEAD.\n"
        f"  - examples: 2–3 SHORT (<60 chars) representative quotes,\n"
        f"              preserve the original review language.\n"
        f"  - review_ids: ARRAY of the [N] integer IDs (the brackets in\n"
        f"                the input) for the N reviews whose PRIMARY\n"
        f"                reason is this. The length MUST equal `count`.\n"
        f"                Every ID must come from the input — never invent.\n"
        f"  Order reasons by count DESC. Skip vague catch-all reasons\n"
        f"  (\"users liked it\", \"it works\") — reasons must be SUBSTANTIVE\n"
        f"  and DISTINCT.\n\n"
        f"For simple_responses:\n"
        f"  - count: how many reviews in the input only stated the outcome\n"
        f"           or gave generic {sub_polarity} feedback without\n"
        f"           explaining why\n"
        f"  - examples: 3–5 SHORT (<60 chars) representative quotes\n"
        f"  - review_ids: same idea — IDs of every outcome-only review.\n\n"
        f"Sum of (reasons[*].count + simple_responses.count) MUST be ≤\n"
        f"total reviews in input. Every review is counted AT MOST ONCE\n"
        f"across reasons AND simple_responses — never duplicate an ID.\n\n"
        f"Output JSON only, no prose, no markdown fences:\n"
        f"{{\n"
        f'  "reasons": [\n'
        f'    {{"reason": "...", "count": N, "examples": ["..."],\n'
        f'      "review_ids": [<int>, <int>, ...]}}\n'
        f"  ],\n"
        f'  "simple_responses": {{"count": N, "examples": ["..."],\n'
        f'                        "review_ids": [<int>, ...]}}\n'
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
    # Reasons
    raw = parsed.get("reasons") or []
    reasons_out: list[dict] = []
    if isinstance(raw, list):
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
            review_ids = _parse_review_ids(item.get("review_ids"))
            reasons_out.append({
                "reason": reason[:120],
                "count": max(0, count),
                "examples": examples,
                "review_ids": review_ids,
            })

    # Simple responses bucket
    sr_raw = parsed.get("simple_responses") or {}
    simple_out = {"count": 0, "examples": [], "review_ids": []}
    if isinstance(sr_raw, dict):
        try:
            simple_out["count"] = max(0, int(sr_raw.get("count") or 0))
        except (TypeError, ValueError):
            simple_out["count"] = 0
        ex_raw = sr_raw.get("examples") or []
        if isinstance(ex_raw, list):
            for e in ex_raw[:5]:
                if e is None:
                    continue
                ex = str(e).strip()
                if ex:
                    simple_out["examples"].append(ex[:120])
        simple_out["review_ids"] = _parse_review_ids(sr_raw.get("review_ids"))

    return {"reasons": reasons_out, "simple_responses": simple_out}


def _parse_review_ids(raw) -> list[int]:
    """Best-effort int conversion of an LLM-supplied review_ids list."""
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for x in raw:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def _sanitize_review_ids(
    reasons: list[dict], simple: dict, sample_ids: set[int]
) -> tuple[list[dict], dict]:
    """Server-side defense against LLM hallucination on review_ids.

      - Drop IDs that weren't in the original sample (the LLM might invent).
      - Dedup across reasons AND simple — every review can belong to one
        bucket at most (matches the prompt rule).
      - First-occurrence wins (preserves the ordering the model produced).
    """
    seen: set[int] = set()
    for r in reasons:
        deduped: list[int] = []
        for rid in r.get("review_ids", []):
            if rid in sample_ids and rid not in seen:
                seen.add(rid)
                deduped.append(rid)
        r["review_ids"] = deduped
    simple_deduped: list[int] = []
    for rid in simple.get("review_ids", []):
        if rid in sample_ids and rid not in seen:
            seen.add(rid)
            simple_deduped.append(rid)
    simple["review_ids"] = simple_deduped
    return reasons, simple


def _cap_counts(reasons: list[dict], simple: dict, sample_size: int) -> tuple[list[dict], dict]:
    """Re-cap reason counts + simple_responses count so the running sum
    stays ≤ sample_size. Reasons are reduced first (in display order);
    if there's still budget left, simple_responses keeps its count.

    Same defensive pattern as the themes.py / competitive.py truth
    override — LLM occasionally over-reports counts in either bucket.
    """
    running = 0
    for r in reasons:
        c = r.get("count", 0)
        if running + c > sample_size:
            r["count"] = max(0, sample_size - running)
        running += r.get("count", 0)
    # Simple bucket eats whatever budget is left.
    simple_count = simple.get("count", 0)
    if running + simple_count > sample_size:
        simple["count"] = max(0, sample_size - running)
    return reasons, simple


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
            "simple_responses": {"count": 0, "examples": []},
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

    llm_out = await _call_claude(
        sample,
        vendor_display=vendor.get("display") or vendor_key,
        category_name=resolved["category_name"],
        category_description=resolved["category_description"],
        band=band,
        summary_lang=summary_lang,
        model=chosen_model,
    )
    # Drop hallucinated review_ids + dedup across buckets BEFORE cap so
    # the counts we trust match the IDs we'll later expand against.
    sample_ids = {r["id"] for r in sample}
    san_reasons, san_simple = _sanitize_review_ids(
        llm_out["reasons"], llm_out["simple_responses"], sample_ids
    )
    # Truth override (bidirectional): the chart bar, the simple-box
    # badge, and the "전체 리뷰 보기 (N)" button MUST agree on the same
    # N. Sanitize already dropped duplicates and hallucinated IDs, so
    # the surviving review_ids list IS the truth. Snap count to its
    # length whenever the LLM declared anything different.
    #
    # Edge case: when sanitize returned 0 valid IDs (older runs / model
    # didn't include any), we leave count alone — falling back to the
    # LLM's self-reported count is better than rewriting it to 0.
    for r in san_reasons:
        ids_n = len(r.get("review_ids", []))
        if ids_n:
            r["count"] = ids_n
    if san_simple.get("review_ids"):
        san_simple["count"] = len(san_simple["review_ids"])
    # Final cap against sample_size — defense against pathological LLMs
    # that still over-report after sanitize.
    capped_reasons, capped_simple = _cap_counts(
        san_reasons, san_simple, len(sample)
    )
    # Hard invariant: count == len(review_ids) for every bucket.
    # _cap_counts can reduce a `count` without trimming review_ids when
    # the running total bumps against sample_size, which leaves the
    # expand button ("N reviews") and the chart bar ("N") disagreeing.
    # Truncate ids to count if count is now smaller. If count somehow
    # ended up larger than ids (shouldn't after sync above, but
    # defensive), trust ids.
    for r in capped_reasons:
        ids = r.get("review_ids", []) or []
        cnt = int(r.get("count") or 0)
        if cnt < len(ids):
            r["review_ids"] = ids[:cnt]
        elif cnt > len(ids):
            r["count"] = len(ids)
    sids = capped_simple.get("review_ids", []) or []
    cnt = int(capped_simple.get("count") or 0)
    if cnt < len(sids):
        capped_simple["review_ids"] = sids[:cnt]
    elif cnt > len(sids) and sids:
        capped_simple["count"] = len(sids)

    result = {
        "vendor_key": vendor_key,
        "vendor_display": vendor.get("display") or vendor_key,
        "category_name": resolved["category_name"],
        "category_description": resolved["category_description"],
        "band": band,
        "source_ids": source_ids,
        "sample_size": len(sample),
        "reasons": capped_reasons,
        "simple_responses": capped_simple,
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
    def _reason_count(stored) -> int:
        # Old shape: list, new shape: {reasons: [...], simple_responses: {...}}
        if isinstance(stored, list):
            return len(stored)
        if isinstance(stored, dict):
            return len(stored.get("reasons") or [])
        return 0
    return [
        {
            "id": c.id,
            "vendor_key": c.vendor_key,
            "vendor_display": c.vendor_display,
            "category_name": c.category_name,
            "band": c.band,
            "label": c.label,
            "sample_size": c.sample_size,
            "reasons_count": _reason_count(c.reasons),
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
    """Persist a fresh extract_reasons() output as a new card row.

    `simple_responses` is stored alongside the `reasons` list inside the
    same JSON column (we wrap into {"reasons": [...], "simple_responses":
    {...}} on save). Older cards saved before this change have a bare
    list as their reasons column; the load path tolerates both shapes.
    """
    vendor_key = (result.get("vendor_key") or "").strip()
    category_name = (result.get("category_name") or "").strip()
    band = (result.get("band") or "").strip()
    if not vendor_key or not category_name or band not in ("positive", "negative"):
        raise ValueError("result is missing vendor_key / category_name / band")
    # Wrap to preserve simple_responses without a schema migration.
    reasons_payload = {
        "reasons": result.get("reasons") or [],
        "simple_responses": result.get("simple_responses") or {"count": 0, "examples": []},
    }
    card = VendorReasonCard(
        vendor_key=vendor_key[:100],
        vendor_display=(result.get("vendor_display") or vendor_key)[:200],
        category_name=category_name[:200],
        band=band,
        label=(label or category_name)[:200],
        model_used=(result.get("model") or "")[:100] or None,
        sample_size=int(result.get("sample_size") or 0),
        source_ids_snapshot=list(result.get("source_ids") or []),
        reasons=reasons_payload,
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
    card.reasons = {
        "reasons": fresh.get("reasons") or [],
        "simple_responses": fresh.get("simple_responses") or {"count": 0, "examples": []},
    }
    card.sample_size = int(fresh.get("sample_size") or 0)
    card.source_ids_snapshot = list(fresh.get("source_ids") or [])
    if fresh.get("model"):
        card.model_used = fresh["model"][:100]
    card.vendor_display = (fresh.get("vendor_display") or card.vendor_display)[:200]
    card.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(card)
    return card
