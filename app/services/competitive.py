"""Competitive-factor ranking.

Given a user-input "competitive factor" (free-form text like "헬스 데이터
기반 AI 코칭"), rank vendors by how well their auto-category STRENGTHS
cover that factor — and, in a separate ranking, surface vendors for whom
the same factor shows up as a WEAKNESS.

Pipeline (one Claude call per submit):

  1. Reuse `list_vendors()` so we inherit all of its safeguards: the
     Wilson-lower-bound positivity score, the single-vendor / non-hidden
     card filter, and the dedup-by-name aggregation. Each vendor's
     `strengths` / `weaknesses` entries already carry the `cat_ids` list
     pointing back at the AutoCategory rows that fed them — used here
     for sample-review lookup.

  2. Build the deduplicated universe of (name, description) across all
     vendors' strengths + weaknesses. This is what we hand to Claude.

  3. One Claude completion: score every (name, description) for how
     directly it matches the factor on a 0-1 scale. Output is a JSON map
     name → relevance.

  4. Per vendor, filter their strengths to those with relevance ≥
     threshold, compute the vendor's overall score as
     `max(relevance × pos_score)`, and attach top-N sample reviews per
     matching strength. Same for weaknesses with `neg_score` and
     negative-sentiment samples.

  5. Sort vendors by score (desc), tiebreak by number of matching
     categories then by analyzed review count.

Sample reviews are fetched eagerly so the UI can expand them without a
round-trip. Per (vendor × match) we issue one query; we cap the per-vendor
match list at 5, so worst-case ≈ 5 × N_vendors queries per submit. The
LLM call is the dominant cost — these sample queries are cheap indexed
lookups.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    Analysis,
    CompetitiveFactorCard,
    Review,
    ReviewAutoCategoryLink,
    Sentiment,
)
from app.services.vendors import (
    SENTIMENT_ORDER,
    _NEG_BAND,
    _POS_BAND,
    list_vendors,
)


# Categories with a relevance below this don't count as "matching" the
# factor at all — we still expose this in the response so the UI can
# show the threshold explanation alongside results.
DEFAULT_THRESHOLD = 0.5

# Floor below which a category is too unrelated to be worth surfacing
# even as a "partial match" debug hint. Anything below this is treated
# as "the LLM is sure this doesn't apply" and dropped silently.
PARTIAL_FLOOR = 0.3

# Hard cap on sample reviews returned per matching category. Three is
# enough to ground the rating in real text without bloating the response.
SAMPLE_LIMIT = 3

# Per-vendor cap on matching categories shown. Beyond 5 the UI gets
# noisy and the lower matches add little signal anyway.
MAX_MATCHES_PER_VENDOR = 5

# How many categories we send to Claude in a single completion. ~110
# distinct names exist in the current dataset. Each scored entry is a
# ~50-token JSON object ({"name": "<korean topic>", "relevance": 0.42}),
# so 30 items fit comfortably in a 4096-token output budget with margin
# for the JSON skeleton. Each batch is a separate LLM call.
LLM_BATCH_SIZE = 30

# Output budget per LLM call. Empirically 30 items × ~50 tokens = 1500,
# plus skeleton overhead. 4096 leaves plenty of headroom even if a future
# prompt makes the per-entry output a bit chattier.
LLM_MAX_TOKENS = 4096


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    return s.strip()


async def _score_categories(
    factor: str, items: list[dict], model: str
) -> dict[str, float]:
    """Ask Claude to score each (name, description) for relevance to factor.

    items = [{"name": str, "description": str|None}, ...]
    Returns {name_lower: relevance ∈ [0,1]}. Names absent from the model
    output default to 0 (no match). Output is keyed on the lowercased
    name so the per-vendor matching can join on it case-insensitively.
    """
    if not items:
        return {}
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    from anthropic import AsyncAnthropic

    system = (
        "You score how directly each topic category matches a user-supplied "
        "competitive factor.\n\n"
        "Scoring rubric (output a real number 0.0–1.0):\n"
        "  1.0  the category IS this factor (essentially the same concept)\n"
        "  0.7  strongly related — a clear sub-aspect or near-synonym\n"
        "  0.4  tangentially related — shares some surface words but\n"
        "       different concept or dimension\n"
        "  0.0  unrelated\n\n"
        "Worked examples:\n"
        '  factor "AI 코칭",                 category "AI 코치 기능"             → 0.95\n'
        '  factor "AI 코칭",                 category "운동 추천"                → 0.50\n'
        '  factor "AI 코칭",                 category "구독 결제"                → 0.00\n'
        '  factor "배터리 수명",              category "Oura 링 배터리 수명"      → 0.95\n'
        '  factor "음식 기록 편의성",         category "사진 촬영 식사 기록"      → 0.85\n'
        '  factor "음식 기록 편의성",         category "AI 칼로리 측정 정확도"    → 0.35\n'
        '  factor "AI 인식 정확도",           category "AI 인식 정확도"           → 1.00\n'
        '  factor "AI 인식 정확도",           category "AI 칼로리 측정 정확도"    → 0.90\n'
        '  factor "Vision AI 인식 정확도",   category "AI 인식 정확도"           → 0.95\n'
        '  factor "Vision AI 인식 정확도",   category "사진 촬영 칼로리 인식"    → 0.50\n'
        '  factor "Vision AI 인식 정확도",   category "UI/UX 디자인 및 내비게이션" → 0.05\n'
        '  factor "구독료 가격",              category "구독·결제"                → 0.85\n'
        '  factor "구독료 가격",              category "구독 취소 후 접근"        → 0.35\n\n'
        "Guidance:\n"
        "  - Match on SEMANTIC meaning, not literal substring overlap.\n"
        "  - Languages may differ between factor and category — treat them\n"
        "    as equivalent (e.g. Korean factor vs English category).\n"
        "  - When the factor highlights a specific DIMENSION (accuracy,\n"
        "    convenience, price, speed, design), a category that covers\n"
        "    the same topic but a DIFFERENT dimension should score LOWER\n"
        "    (around 0.3–0.5), not the same as a dimension-match.\n"
        "  - UI/UX/design/navigation categories are UNRELATED to AI\n"
        "    accuracy / coaching / tracking unless the factor explicitly\n"
        "    mentions the interface.\n"
        "  - If the factor uses the SAME wording as a category, that\n"
        "    category must score 0.95+ — never penalize an exact match.\n\n"
        "Output JSON only:\n"
        '  {"scores": [{"name": "<exact input name>", "relevance": <0..1>}, ...]}\n'
        "Include EVERY input category exactly once. No prose, no markdown."
    )
    user = (
        f'Competitive factor: "{factor}"\n\n'
        "Categories to score:\n"
        + "\n".join(
            f"- {it['name']}"
            + (f" — {it['description']}" if it.get("description") else "")
            for it in items
        )
    )

    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    resp = await client.messages.create(
        model=model,
        max_tokens=LLM_MAX_TOKENS,
        # temperature=0 pins the LLM to its most-likely scoring path so
        # the same (factor, universe) pair returns the same scores across
        # calls. Without it Haiku samples at ~0.7 and borderline matches
        # (around the 0.5 threshold) flip in/out of the ranking between
        # consecutive submits, which surfaces as "why was SnapCalorie
        # included last time but missing now?" confusion.
        temperature=0.0,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = _strip_fences(
        "".join(getattr(b, "text", "") for b in resp.content)
    )
    # Defensive parse: if Claude truncates output (very unlikely now that
    # we batch 30 items into a 4096-token budget, but the failure mode is
    # ugly enough — the whole request returned 400 — that we'd rather
    # degrade gracefully). On parse failure, return an empty score map
    # for this batch; downstream code treats unknown names as relevance=0.
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    out: dict[str, float] = {}
    for s in parsed.get("scores", []) or []:
        if not isinstance(s, dict):
            continue
        name = (s.get("name") or "").strip().lower()
        if not name:
            continue
        try:
            r = float(s.get("relevance"))
        except (TypeError, ValueError):
            continue
        out[name] = max(0.0, min(1.0, r))
    return out


async def _fetch_samples(
    session: AsyncSession,
    cat_ids: list[int],
    source_ids: list[int],
    sentiments: set[str],
    *,
    order_desc: bool,
    limit: int,
) -> list[dict]:
    """Top N review samples joining junction → analysis → review.

    Scoped to the given AutoCategory ids AND vendor's source ids AND a
    sentiment band (positive band for strengths, negative for weaknesses).
    Ordered by sentiment_score DESC for strengths (strongest praise first)
    or ASC for weaknesses (strongest complaint first); review id breaks
    ties so the picks are stable across reloads.
    """
    if not cat_ids or not source_ids or not sentiments:
        return []
    sentiment_values = [Sentiment(s) for s in sentiments]
    order_col = (
        Analysis.sentiment_score.desc().nullslast()
        if order_desc
        else Analysis.sentiment_score.asc().nullsfirst()
    )
    rows = (
        await session.execute(
            select(
                Review.id,
                Review.text,
                Review.rating,
                Analysis.sentiment,
                Analysis.sentiment_score,
            )
            .join(Analysis, Analysis.review_id == Review.id)
            .join(
                ReviewAutoCategoryLink,
                ReviewAutoCategoryLink.c.review_id == Review.id,
            )
            .where(ReviewAutoCategoryLink.c.auto_category_id.in_(cat_ids))
            .where(Review.source_id.in_(source_ids))
            .where(Analysis.sentiment.in_(sentiment_values))
            .order_by(order_col, Review.id.desc())
            .limit(limit)
        )
    ).all()
    out: list[dict] = []
    seen: set[int] = set()
    for rid, text, rating, sent, sscore in rows:
        if rid in seen:
            continue
        seen.add(rid)
        s_key = sent.value if hasattr(sent, "value") else str(sent)
        out.append({
            "id": int(rid),
            "text": (text or "")[:300],
            "rating": int(rating) if rating is not None else None,
            "sentiment": s_key,
            "sentiment_score": int(sscore) if sscore is not None else None,
        })
    return out


async def rank_vendors_by_factor(
    session: AsyncSession,
    factor: str,
    *,
    model: Optional[str] = None,
    threshold: float = DEFAULT_THRESHOLD,
    sample_limit: int = SAMPLE_LIMIT,
) -> dict:
    """Main entry point used by the /api/competitive-rank route.

    Returns a dict the partial template can iterate directly — see the
    module docstring for the response shape.
    """
    factor = (factor or "").strip()
    if not factor:
        raise ValueError("factor is required")
    threshold = max(0.0, min(1.0, float(threshold)))

    chosen_model = (model or settings.ANTHROPIC_MODEL).strip()

    # ---- 1. Vendor roll-up (reuses Wilson + single-vendor scoping) ----
    vendors = await list_vendors(session)

    # ---- 2. Build deduped (name, description) universe ----
    universe: dict[str, dict] = {}
    for v in vendors:
        for s in v.get("strengths", []) + v.get("weaknesses", []):
            key = (s.get("name") or "").strip().lower()
            if not key:
                continue
            if key in universe:
                continue
            universe[key] = {
                "name": s["name"],
                "description": s.get("description"),
            }

    if not universe:
        return {
            "factor": factor,
            "threshold": threshold,
            "score_formula": "max(relevance × Wilson_pos_score)",
            "strength_ranking": [],
            "weakness_ranking": [],
            "partial_strength_matches": [],
            "partial_weakness_matches": [],
            "universe_size": 0,
            "message": (
                "분석된 자동 카테고리가 없습니다. 먼저 대시보드에서 카드를 "
                "분석해주세요."
            ),
        }

    # ---- 3. LLM scoring (batched) ----
    items = list(universe.values())
    scores: dict[str, float] = {}
    for i in range(0, len(items), LLM_BATCH_SIZE):
        batch = items[i : i + LLM_BATCH_SIZE]
        partial = await _score_categories(factor, batch, chosen_model)
        scores.update(partial)

    # ---- 4. Per-vendor matching + sample fetch ----
    strength_rows: list[dict] = []
    weakness_rows: list[dict] = []
    # Vendors whose best strength/weakness fell in [PARTIAL_FLOOR, threshold).
    # Surfaced in a collapsed "🔍 부분 매칭" panel so the user can see WHY
    # a vendor they expected (e.g. SnapCalorie) didn't reach the cutoff.
    partial_strength_rows: list[dict] = []
    partial_weakness_rows: list[dict] = []

    for v in vendors:
        src_ids = v.get("source_ids") or []
        if not src_ids:
            continue

        # ---- Strengths side ----
        s_matches: list[dict] = []
        s_partial_best: Optional[dict] = None  # best below-threshold candidate
        for s in v.get("strengths", []):
            r = scores.get((s.get("name") or "").strip().lower(), 0.0)
            if r < threshold:
                # Track for the partial-matches panel without fetching
                # samples (cheap; samples only for the main ranking).
                if r >= PARTIAL_FLOOR:
                    candidate = {
                        "name": s["name"],
                        "relevance": round(r, 3),
                        "pos_pct": round(float(s.get("pos_pct") or 0.0), 4),
                        "pos_score": round(float(s.get("pos_score") or 0.0), 4),
                        "total": int(s.get("total") or 0),
                    }
                    if not s_partial_best or candidate["relevance"] > s_partial_best["relevance"]:
                        s_partial_best = candidate
                continue
            pos_score = float(s.get("pos_score") or 0.0)
            match_score = r * pos_score
            samples = await _fetch_samples(
                session,
                cat_ids=s.get("cat_ids") or [],
                source_ids=src_ids,
                sentiments=_POS_BAND,
                order_desc=True,
                limit=sample_limit,
            )
            s_matches.append({
                "name": s["name"],
                "description": s.get("description"),
                "relevance": round(r, 3),
                "pos_pct": round(float(s.get("pos_pct") or 0.0), 4),
                "pos_score": round(pos_score, 4),
                "total": int(s.get("total") or 0),
                "small_sample": bool(s.get("small_sample")),
                "match_score": round(match_score, 4),
                "samples": samples,
            })
        s_matches.sort(key=lambda m: m["match_score"], reverse=True)
        s_matches = s_matches[:MAX_MATCHES_PER_VENDOR]
        vendor_dict = {
            "key": v["key"],
            "display": v["display"],
            "icon_url": v.get("icon_url"),
            "source_ids": src_ids,
            "review_count": v.get("review_count"),
            "analyzed_count": v.get("analyzed_count"),
            "avg_rating": v.get("avg_rating"),
            "platforms": v.get("platforms", []),
        }
        if s_matches:
            top = s_matches[0]
            strength_rows.append({
                "vendor": vendor_dict,
                "score": top["match_score"],
                "score_breakdown": {
                    "relevance": top["relevance"],
                    "pos_score": top["pos_score"],
                    "category": top["name"],
                },
                "matches": s_matches,
            })
        elif s_partial_best:
            # Vendor didn't qualify for the main ranking but has a
            # near-miss. Surface so the user can audit.
            partial_strength_rows.append({
                "vendor": vendor_dict,
                "top_partial": s_partial_best,
            })

        # ---- Weaknesses side ----
        w_matches: list[dict] = []
        w_partial_best: Optional[dict] = None
        for w in v.get("weaknesses", []):
            r = scores.get((w.get("name") or "").strip().lower(), 0.0)
            if r < threshold:
                if r >= PARTIAL_FLOOR:
                    candidate = {
                        "name": w["name"],
                        "relevance": round(r, 3),
                        "neg_pct": round(float(w.get("neg_pct") or 0.0), 4),
                        "neg_score": round(float(w.get("neg_score") or 0.0), 4),
                        "total": int(w.get("total") or 0),
                    }
                    if not w_partial_best or candidate["relevance"] > w_partial_best["relevance"]:
                        w_partial_best = candidate
                continue
            neg_score = float(w.get("neg_score") or 0.0)
            match_score = r * neg_score
            samples = await _fetch_samples(
                session,
                cat_ids=w.get("cat_ids") or [],
                source_ids=src_ids,
                sentiments=_NEG_BAND,
                order_desc=False,  # ASC: strongest complaint (lowest score) first
                limit=sample_limit,
            )
            w_matches.append({
                "name": w["name"],
                "description": w.get("description"),
                "relevance": round(r, 3),
                "neg_pct": round(float(w.get("neg_pct") or 0.0), 4),
                "neg_score": round(neg_score, 4),
                "total": int(w.get("total") or 0),
                "small_sample": bool(w.get("small_sample")),
                "match_score": round(match_score, 4),
                "samples": samples,
            })
        w_matches.sort(key=lambda m: m["match_score"], reverse=True)
        w_matches = w_matches[:MAX_MATCHES_PER_VENDOR]
        if w_matches:
            top = w_matches[0]
            weakness_rows.append({
                "vendor": vendor_dict,
                "score": top["match_score"],
                "score_breakdown": {
                    "relevance": top["relevance"],
                    "neg_score": top["neg_score"],
                    "category": top["name"],
                },
                "matches": w_matches,
            })
        elif w_partial_best:
            partial_weakness_rows.append({
                "vendor": vendor_dict,
                "top_partial": w_partial_best,
            })

    # ---- 5. Sort ----
    strength_rows.sort(
        key=lambda r: (r["score"], len(r["matches"]), r["vendor"].get("analyzed_count") or 0),
        reverse=True,
    )
    weakness_rows.sort(
        key=lambda r: (r["score"], len(r["matches"]), r["vendor"].get("analyzed_count") or 0),
        reverse=True,
    )

    # Sort partial-match lists by relevance desc so the closest near-misses
    # surface first in the collapsed debug panel.
    partial_strength_rows.sort(
        key=lambda r: r["top_partial"]["relevance"], reverse=True
    )
    partial_weakness_rows.sort(
        key=lambda r: r["top_partial"]["relevance"], reverse=True
    )

    return {
        "factor": factor,
        "threshold": threshold,
        "score_formula": "max(relevance × Wilson_pos_score)",
        "strength_ranking": strength_rows,
        "weakness_ranking": weakness_rows,
        "partial_strength_matches": partial_strength_rows,
        "partial_weakness_matches": partial_weakness_rows,
        "universe_size": len(universe),
    }


# ============================================================================
# Saved-card CRUD
# ============================================================================
#
# These wrap the CompetitiveFactorCard model so the routes stay
# transport-layer-only. Snapshots are point-in-time — no upsert by
# factor, every save creates a new row, the user manages duplicates.


async def list_saved_cards(
    session: AsyncSession, *, include_hidden: bool = False
) -> list[dict]:
    """Sidebar listing — metadata only, no row payload."""
    stmt = select(CompetitiveFactorCard)
    if not include_hidden:
        stmt = stmt.where(CompetitiveFactorCard.hidden.is_(False))
    stmt = stmt.order_by(
        CompetitiveFactorCard.display_order.asc(),
        CompetitiveFactorCard.updated_at.desc(),
    )
    rows = (await session.execute(stmt)).scalars().all()
    out: list[dict] = []
    for c in rows:
        input_csv = c.input_csv or []
        result_payload = c.result_rows or {}
        # Total matched = sum of per-group counts. Tolerate both old
        # shape (bare list) and new (dict with groups).
        if isinstance(result_payload, dict):
            total_matched = int(result_payload.get("total_matched_rows") or 0)
            if not total_matched and "groups" in result_payload:
                total_matched = sum(
                    len(g.get("result_rows") or [])
                    for g in (result_payload.get("groups") or [])
                )
        elif isinstance(result_payload, list):
            total_matched = len(result_payload)
        else:
            total_matched = 0
        factors_list = _card_factors(c)
        out.append({
            "id": c.id,
            "factor": c.factor,
            "factors": factors_list,
            "factor_count": len(factors_list),
            "label": c.label,
            "threshold": c.threshold,
            "model_used": c.model_used,
            "hidden": c.hidden,
            "display_order": c.display_order,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            "input_row_count": len(input_csv),
            "result_row_count": total_matched,
        })
    return out


async def get_saved_card(
    session: AsyncSession, card_id: int
) -> Optional[CompetitiveFactorCard]:
    return await session.get(CompetitiveFactorCard, card_id)


async def save_card(
    session: AsyncSession,
    *,
    factors: list[str],
    label: Optional[str],
    input_csv: list[dict],
    result_payload: dict,
    threshold: float,
    model_used: Optional[str],
) -> CompetitiveFactorCard:
    """Persist a multi-factor CSV-driven analysis as a new card.

    `factors` is the full list; `result_payload` is the analyze_csv()
    dict (with `groups` etc.) — saved as-is in result_rows so loads
    can render without recomputing anything.
    """
    if not isinstance(factors, list) or not factors:
        raise ValueError("at least one factor is required")
    if not isinstance(input_csv, list) or not isinstance(result_payload, dict):
        raise ValueError("input_csv must be a list and result_payload a dict")
    # Drop empties + dedupe, mirror analyze_csv normalization.
    cleaned_factors: list[str] = []
    seen: set[str] = set()
    for f in factors:
        s = (str(f) if f is not None else "").strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned_factors.append(s[:200])
    if not cleaned_factors:
        raise ValueError("at least one non-empty factor is required")

    max_order = (
        await session.execute(
            select(CompetitiveFactorCard.display_order)
            .order_by(CompetitiveFactorCard.display_order.desc())
            .limit(1)
        )
    ).scalar_one_or_none() or 0

    primary_factor = cleaned_factors[0]
    card = CompetitiveFactorCard(
        factor=primary_factor[:200],
        factors=cleaned_factors,
        label=(label or primary_factor)[:200],
        threshold=float(threshold),
        model_used=(model_used or "")[:100] or None,
        input_csv=input_csv,
        # result_payload contains `groups`, `total_matched_rows`, etc.
        # Stored verbatim under result_rows so the load path is a no-op.
        result_rows=result_payload,
        universe_size=0,
        result={},
        hidden=False,
        display_order=int(max_order) + 1,
    )
    session.add(card)
    await session.commit()
    await session.refresh(card)
    return card


def _card_factors(card: CompetitiveFactorCard) -> list[str]:
    """Read a card's factor list, tolerating the legacy single-factor
    shape (no `factors` column populated)."""
    if card.factors:
        return [str(f) for f in card.factors if str(f).strip()]
    if card.factor:
        return [card.factor]
    return []


async def update_card_label(
    session: AsyncSession, card_id: int, label: str
) -> Optional[CompetitiveFactorCard]:
    card = await session.get(CompetitiveFactorCard, card_id)
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
) -> Optional[CompetitiveFactorCard]:
    card = await session.get(CompetitiveFactorCard, card_id)
    if not card:
        return None
    card.hidden = bool(hidden)
    await session.commit()
    await session.refresh(card)
    return card


async def delete_card(session: AsyncSession, card_id: int) -> bool:
    card = await session.get(CompetitiveFactorCard, card_id)
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
) -> Optional[CompetitiveFactorCard]:
    """Re-run analyze_csv() using the card's saved input_csv + factors +
    threshold; overwrite result_rows + bump updated_at."""
    card = await session.get(CompetitiveFactorCard, card_id)
    if not card:
        return None
    input_csv = card.input_csv or []
    if not input_csv:
        return card
    new = await analyze_csv(
        factors=_card_factors(card),
        rows=input_csv,
        threshold=card.threshold,
        model=model,
    )
    card.result_rows = new
    card.factors = new.get("factors")
    if model:
        card.model_used = model[:100]
    card.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(card)
    return card


async def compute_drift(
    session: AsyncSession, card: CompetitiveFactorCard
) -> dict:
    """How much has the universe of auto-categories changed since this
    card was saved? Used to flag "your saved view may be stale" in the UI.

    Returns:
      {
        "saved_universe": int,   # at save time
        "current_universe": int, # right now
        "delta": int,            # current - saved
      }
    """
    vendors = await list_vendors(session)
    universe: set[str] = set()
    for v in vendors:
        for s in v.get("strengths", []) + v.get("weaknesses", []):
            n = (s.get("name") or "").strip().lower()
            if n:
                universe.add(n)
    current = len(universe)
    saved = int(card.universe_size or 0)
    return {
        "saved_universe": saved,
        "current_universe": current,
        "delta": current - saved,
    }


async def reorder_cards(session: AsyncSession, ordered_ids: list[int]) -> int:
    """Assign 1..N display_order to the given card ids in array position.

    Cards not in the list keep their current order; this is how the UI
    sends just the visible reordered subset. Returns the number of rows
    actually updated."""
    if not ordered_ids:
        return 0
    n_updated = 0
    for i, cid in enumerate(ordered_ids, start=1):
        card = await session.get(CompetitiveFactorCard, int(cid))
        if not card:
            continue
        if card.display_order != i:
            card.display_order = i
            n_updated += 1
    if n_updated:
        await session.commit()
    return n_updated


# ============================================================================
# CSV-driven analysis (new flow)
# ============================================================================
#
# Replaces the DB-querying rank_vendors_by_factor flow with one that
# operates entirely on a user-uploaded CSV (typically the output of
# /vendors/export.csv). The benefit: the user picks exactly which
# vendors + which sides go into the comparison, and the analysis stays
# tied to a snapshot regardless of how the underlying DB drifts.


def _normalize_csv_row(raw: dict) -> Optional[dict]:
    """Coerce / validate one CSV row.

    Returns the cleaned dict, or None if the row is malformed beyond
    repair (missing vendor or category). All keys preserved verbatim
    in the output so the UI can render the rest of the columns even
    if some are empty.
    """
    if not isinstance(raw, dict):
        return None
    vendor = str(raw.get("vendor") or "").strip()
    category = str(raw.get("category") or "").strip()
    if not vendor or not category:
        return None
    row_type = str(raw.get("type") or "").strip().lower()
    try:
        pct = float(raw.get("pct") or 0)
    except (TypeError, ValueError):
        pct = 0.0
    try:
        count = int(raw.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    try:
        wilson_score = float(raw.get("wilson_score") or 0)
    except (TypeError, ValueError):
        wilson_score = 0.0
    return {
        "vendor": vendor[:200],
        "type": row_type[:32] if row_type else "strength",
        "category": category[:200],
        "pct": pct,
        "count": count,
        "wilson_score": wilson_score,
        "description": str(raw.get("description") or "").strip()[:500],
        "small_sample": bool(raw.get("small_sample")) if not isinstance(raw.get("small_sample"), str) else (raw.get("small_sample") or "").strip().upper() == "Y",
        "reasons": str(raw.get("reasons") or "").strip()[:2000],
    }


# Hard cap on the number of factors a single analysis can score
# against. Each factor is a separate Claude call (batched) so 10 is
# already 10× the cost of a single-factor analysis. Beyond that the
# UI also becomes hard to scan.
MAX_FACTORS = 10


async def _score_factors_parallel(
    factors: list[str],
    items: list[dict],
    model: str,
) -> dict[str, dict[str, float]]:
    """Score each factor against the same category universe in parallel.

    Returns {factor: {category_name_lower: relevance}}. Each factor
    becomes one (or more, when len(items) > LLM_BATCH_SIZE) Claude
    completion; asyncio.gather runs them concurrently so total wall-
    clock ≈ slowest single factor instead of sum.
    """
    import asyncio

    async def _one(factor: str) -> dict[str, float]:
        scores: dict[str, float] = {}
        for i in range(0, len(items), LLM_BATCH_SIZE):
            batch = items[i : i + LLM_BATCH_SIZE]
            partial = await _score_categories(factor, batch, model)
            scores.update(partial)
        return scores

    results = await asyncio.gather(*(_one(f) for f in factors))
    return dict(zip(factors, results))


async def analyze_csv(
    *,
    factors: list[str],
    rows: list[dict],
    threshold: float = DEFAULT_THRESHOLD,
    model: Optional[str] = None,
) -> dict:
    """Score a CSV against MULTIPLE competitive factors and group the
    matched rows under each factor.

    Pipeline:
      1. Normalize / dedup input factors (strip, max MAX_FACTORS).
      2. Coerce CSV rows + filter to type='strength'.
      3. Build the distinct category universe.
      4. Parallel LLM calls (one per factor, batched within).
      5. Per factor, filter rows ≥ threshold and sort relevance ↓
         then pct ↓. A row that matches N factors appears N times
         (once per group) — that's the user's intent: "classify into
         each competitive factor".

    Returns:
      {
        "factors": [str, ...],
        "threshold": float,
        "input_row_count": int,
        "strength_input_count": int,
        "groups": [
          {"factor": str, "result_rows": [...], "matched_count": int}, ...
        ],
        "total_matched_rows": int,         # sum across groups (can double-count)
        "universe_size": int,
        "model": str,
      }
    """
    # 1. Normalize factors
    if not isinstance(factors, list):
        raise ValueError("factors must be a list")
    cleaned_factors: list[str] = []
    seen: set[str] = set()
    for f in factors:
        s = (str(f) if f is not None else "").strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned_factors.append(s[:200])
        if len(cleaned_factors) >= MAX_FACTORS:
            break
    if not cleaned_factors:
        raise ValueError("at least one factor is required")
    threshold = max(0.0, min(1.0, float(threshold)))

    # 2. CSV row normalize + strength filter
    cleaned: list[dict] = []
    for r in rows or []:
        norm = _normalize_csv_row(r)
        if norm:
            cleaned.append(norm)
    input_row_count = len(cleaned)
    strengths = [r for r in cleaned if r["type"] == "strength"]

    if not strengths:
        return {
            "factors": cleaned_factors,
            "threshold": threshold,
            "input_row_count": input_row_count,
            "strength_input_count": 0,
            "groups": [
                {"factor": f, "result_rows": [], "matched_count": 0}
                for f in cleaned_factors
            ],
            "total_matched_rows": 0,
            "universe_size": 0,
            "model": (model or settings.ANTHROPIC_MODEL),
            "message": "no_strength_rows_in_csv",
        }

    # 3. Distinct universe
    universe: dict[str, dict] = {}
    for r in strengths:
        key = r["category"].strip().lower()
        if key in universe:
            continue
        universe[key] = {
            "name": r["category"],
            "description": r["description"] or None,
        }
    items = list(universe.values())

    # 4. Parallel LLM scoring per factor
    chosen_model = (model or settings.ANTHROPIC_MODEL).strip()
    per_factor_scores = await _score_factors_parallel(
        cleaned_factors, items, chosen_model
    )

    # 5. Build per-factor groups
    groups: list[dict] = []
    total_matched = 0
    for f in cleaned_factors:
        scores = per_factor_scores.get(f, {})
        matched: list[dict] = []
        for r in strengths:
            cat_key = r["category"].strip().lower()
            relevance = float(scores.get(cat_key, 0.0))
            if relevance < threshold:
                continue
            out = dict(r)
            out["relevance"] = round(relevance, 3)
            out["match_score"] = round(relevance * r["wilson_score"], 4)
            matched.append(out)
        matched.sort(key=lambda x: (x["relevance"], x["pct"]), reverse=True)
        groups.append({
            "factor": f,
            "result_rows": matched,
            "matched_count": len(matched),
        })
        total_matched += len(matched)

    return {
        "factors": cleaned_factors,
        "threshold": threshold,
        "input_row_count": input_row_count,
        "strength_input_count": len(strengths),
        "groups": groups,
        "total_matched_rows": total_matched,
        "universe_size": len(universe),
        "model": chosen_model,
    }
