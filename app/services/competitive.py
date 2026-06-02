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
        "CRITICAL — DIMENSION RULE:\n"
        "  A category and a factor can share the same TOPIC but evaluate\n"
        "  different DIMENSIONS of it (convenience / accuracy / speed /\n"
        "  price / availability / aesthetics). When the dimensions\n"
        "  differ, score LOW (≤ 0.45) even if the topic words overlap.\n"
        "  Only categories that match BOTH the topic AND the dimension\n"
        "  earn ≥ 0.7.\n\n"
        "Examples (study the dimension column):\n"
        '  factor "AI 코칭",                 category "AI 코치 기능"           → 0.95   (same topic+dim)\n'
        '  factor "AI 코칭",                 category "운동 추천"              → 0.50   (related topic)\n'
        '  factor "AI 코칭",                 category "구독 결제"              → 0.00   (unrelated)\n'
        '  factor "배터리 수명",              category "Oura 링 배터리 수명"    → 0.95   (same)\n'
        '  factor "음식 기록 편의성"   [편의], category "사진 촬영 식사 기록"   → 0.85   (convenience ↔ convenience)\n'
        '  factor "음식 기록 편의성"   [편의], category "AI 칼로리 측정 정확도" → 0.30   (different DIM: 정확도)\n'
        '  factor "Vision AI 인식 정확도" [정확], category "AI 인식 정확도"     → 0.95   (accuracy ↔ accuracy)\n'
        '  factor "Vision AI 인식 정확도" [정확], category "사진 촬영 칼로리 인식" → 0.40   (different DIM: 편의)\n'
        '  factor "Vision AI 인식 정확도" [정확], category "UI/UX 디자인"       → 0.05   (unrelated)\n'
        '  factor "구독료 가격",       [가격], category "구독·결제"             → 0.85\n'
        '  factor "구독료 가격",       [가격], category "구독 취소 후 접근"     → 0.30   (different DIM)\n\n'
        "Match on SEMANTIC meaning, not literal substring overlap. Languages\n"
        "may differ between factor and category — treat them as equivalent\n"
        "(e.g. Korean factor vs English category, or vice versa). UI/UX,\n"
        "design, navigation are unrelated to AI accuracy / coaching /\n"
        "tracking unless the factor explicitly mentions interface.\n\n"
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
    """Sidebar listing — metadata only, no `result` JSON in the payload.

    Keeps the response small (~200 bytes/row) so the sidebar can refresh
    on every save/delete without hauling around the full result blobs.
    The full row is fetched on click via `get_saved_card`.
    """
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
        result = c.result or {}
        out.append({
            "id": c.id,
            "factor": c.factor,
            "label": c.label,
            "threshold": c.threshold,
            "universe_size": c.universe_size,
            "model_used": c.model_used,
            "hidden": c.hidden,
            "display_order": c.display_order,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            # Pre-computed counts so the sidebar can render the
            # "N vendors" / "M weakness signals" pills without parsing
            # the result JSON in the template.
            "strength_count": len(result.get("strength_ranking") or []),
            "weakness_count": len(result.get("weakness_ranking") or []),
        })
    return out


async def get_saved_card(
    session: AsyncSession, card_id: int
) -> Optional[CompetitiveFactorCard]:
    return await session.get(CompetitiveFactorCard, card_id)


async def save_card(
    session: AsyncSession,
    *,
    factor: str,
    label: Optional[str],
    result: dict,
    threshold: float,
    model_used: Optional[str],
) -> CompetitiveFactorCard:
    """Persist a fresh analysis. Always creates a new row (no upsert).

    `universe_size` and the embedded threshold are pulled out of
    `result` if not provided explicitly so the caller can just pass the
    rank_vendors_by_factor() return verbatim.
    """
    factor = (factor or "").strip()
    if not factor:
        raise ValueError("factor is required")
    if not isinstance(result, dict):
        raise ValueError("result must be a dict")

    # Place new cards at the end of the visible list. Read max+1
    # rather than COUNT(*) so hidden cards don't disturb ordering.
    max_order = (
        await session.execute(
            select(CompetitiveFactorCard.display_order)
            .order_by(CompetitiveFactorCard.display_order.desc())
            .limit(1)
        )
    ).scalar_one_or_none() or 0

    card = CompetitiveFactorCard(
        factor=factor[:200],
        label=(label or factor)[:200],
        threshold=float(threshold),
        model_used=(model_used or "")[:100] or None,
        universe_size=int(result.get("universe_size") or 0),
        result=result,
        hidden=False,
        display_order=int(max_order) + 1,
    )
    session.add(card)
    await session.commit()
    await session.refresh(card)
    return card


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
    """Re-run rank_vendors_by_factor() with the card's saved factor +
    threshold and overwrite this row's `result` + bump `updated_at`.

    The model arg lets the user pick a different Claude tier without
    losing the saved threshold or factor. universe_size is refreshed
    from the new run so the drift indicator stays accurate."""
    card = await session.get(CompetitiveFactorCard, card_id)
    if not card:
        return None
    new_result = await rank_vendors_by_factor(
        session,
        card.factor,
        model=model,
        threshold=card.threshold,
        sample_limit=SAMPLE_LIMIT,
    )
    card.result = new_result
    card.universe_size = int(new_result.get("universe_size") or 0)
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
