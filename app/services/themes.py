"""LLM-extracted reasons-behind-sentiment for the dashboard mind-map panel.

For a chosen sentiment band (e.g. very_positive) plus the current dashboard
filters, sample up to N matching reviews, send their summaries to Claude, and
ask for themes GROUPED BY CATEGORY. Returns:

  {
    "sentiment": "...",
    "sample_size": 87,
    "categories": [
      {
        "category": "UX > Onboarding",
        "total": 28,                      # reviews in this category from the sample
        "themes": [
          {"theme": "...", "count": 12, "examples": ["...", "..."]},
          ...
        ]
      },
      ...
    ],
    "themes": [...],   # flat fallback for legacy renderers
    "model": "...", "generated_at": ..., "cached": bool
  }

Counts within one category should sum to <= category total — the prompt
explicitly asks for the PRIMARY theme per review so a review is counted once.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Analysis, Category, Review, Sentiment
from app.services.stats import _descendants_of, _normalize_ids

SAMPLE_SIZE = 100
CACHE_TTL = 600  # 10 minutes
UNCATEGORIZED_LABEL = "Uncategorized"

_cache: dict[str, tuple[float, dict]] = {}


def _cache_key(sentiment: str, source_ids, root_ids, summary_lang: str) -> str:
    src = sorted(source_ids or [])
    rts = sorted(root_ids or [])
    raw = f"{sentiment}|{src}|{rts}|{summary_lang}|v2"  # v2 marks new schema
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _get_cached(key: str) -> Optional[dict]:
    entry = _cache.get(key)
    if not entry:
        return None
    ts, data = entry
    if time.monotonic() - ts > CACHE_TTL:
        _cache.pop(key, None)
        return None
    return data


def _set_cached(key: str, data: dict) -> None:
    _cache[key] = (time.monotonic(), data)


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
    return s.strip()


async def _call_claude(sample, sentiment: Sentiment, summary_lang: str, model: str) -> dict:
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    from anthropic import AsyncAnthropic

    lang_label = {"ko": "Korean", "en": "English"}.get(summary_lang, "English")

    system = (
        f"You analyze user reviews to surface the main reasons behind a specific sentiment.\n"
        f"All reviews below share sentiment: {sentiment.value}\n"
        f"Each review is tagged with the category path it was classified into.\n\n"
        f"GROUP your analysis BY CATEGORY. For each category that has >= 3 reviews:\n"
        f"  - Identify 3–5 distinct themes within that category\n"
        f"  - For each theme:\n"
        f"      * theme: short label in {lang_label}, 2–5 words\n"
        f"      * count: number of reviews whose PRIMARY topic is this theme.\n"
        f"        IMPORTANT: count each review once under its strongest theme;\n"
        f"        the sum of counts within one category MUST be <= that category's review count.\n"
        f"      * examples: 2–3 SHORT (<50 chars) representative quotes,\n"
        f"        preserve the review's original language\n\n"
        f"Skip categories with fewer than 3 reviews unless that's the only data.\n\n"
        f"Respond with ONLY a JSON object, no prose, no markdown fences:\n"
        f"{{\n"
        f'  "categories": [\n'
        f'    {{\n'
        f'      "category": "<category path or Uncategorized>",\n'
        f'      "total": <int, reviews in this category from the sample>,\n'
        f'      "themes": [\n'
        f'        {{"theme": "...", "count": N, "examples": ["...", "..."]}}\n'
        f'      ]\n'
        f'    }}\n'
        f'  ]\n'
        f"}}"
    )

    user_msg = "Reviews:\n" + "\n".join(
        f"[{r['id']}] (category: {r['category']}) {r['snippet']}" for r in sample
    )

    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    resp = await client.messages.create(
        model=model,
        max_tokens=3000,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(getattr(b, "text", "") for b in resp.content)
    text = _strip_fences(text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"failed to parse LLM JSON: {e}; raw={text[:200]!r}")

    if not isinstance(parsed, dict):
        return {"categories": []}

    raw_cats = parsed.get("categories") or []
    if not isinstance(raw_cats, list):
        return {"categories": []}

    categories: list[dict] = []
    for cat in raw_cats:
        if not isinstance(cat, dict):
            continue
        cat_name = str(cat.get("category") or "").strip()
        if not cat_name:
            continue
        try:
            total = int(cat.get("total")) if cat.get("total") is not None else None
        except (TypeError, ValueError):
            total = None

        raw_themes = cat.get("themes") or []
        themes_out: list[dict] = []
        if isinstance(raw_themes, list):
            for item in raw_themes:
                if not isinstance(item, dict):
                    continue
                theme = str(item.get("theme") or "").strip()
                if not theme:
                    continue
                try:
                    count = int(item.get("count")) if item.get("count") is not None else None
                except (TypeError, ValueError):
                    count = None
                examples_raw = item.get("examples") or []
                examples: list[str] = []
                if isinstance(examples_raw, list):
                    for e in examples_raw[:3]:
                        if e is None:
                            continue
                        examples.append(str(e).strip()[:80])
                themes_out.append({"theme": theme[:80], "count": count, "examples": examples})

        # Cap individual theme counts so their sum doesn't exceed total — defensive
        # in case the model didn't follow the instruction.
        if total is not None and themes_out:
            running = 0
            for t in themes_out:
                c = t.get("count")
                if isinstance(c, int):
                    if running + c > total:
                        t["count"] = max(0, total - running)
                    running += t["count"] if isinstance(t["count"], int) else 0

        categories.append({"category": cat_name[:120], "total": total, "themes": themes_out})

    return {"categories": categories}


def _flatten(categories: list[dict]) -> list[dict]:
    """Legacy flat themes list — concatenation of all categories' themes,
    annotated with their parent category."""
    out: list[dict] = []
    for cat in categories:
        for t in cat.get("themes") or []:
            out.append({
                "theme": t.get("theme"),
                "count": t.get("count"),
                "examples": t.get("examples") or [],
                "category": cat.get("category"),
            })
    return out


async def extract_themes(
    session: AsyncSession,
    sentiment: str,
    source_ids: Optional[Sequence[int]] = None,
    root_ids: Optional[Sequence[int]] = None,
    summary_lang: str = "en",
    model: Optional[str] = None,
    force: bool = False,
) -> dict:
    key = _cache_key(sentiment, source_ids, root_ids, summary_lang)
    if not force:
        cached = _get_cached(key)
        if cached:
            return {**cached, "cached": True}

    try:
        sent_enum = Sentiment(sentiment)
    except ValueError:
        return {"sentiment": sentiment, "categories": [], "themes": [], "sample_size": 0, "error": "invalid sentiment"}

    src_ids = _normalize_ids(source_ids)
    selected_roots = _normalize_ids(root_ids)

    cat_filter: Optional[set[int]] = None
    if selected_roots:
        all_cats = (await session.execute(select(Category))).scalars().all()
        parent_by_id = {c.id: c.parent_id for c in all_cats}
        cat_filter = _descendants_of(parent_by_id, selected_roots)

    stmt = (
        select(Review.id, Review.text, Analysis.summary, Category.path)
        .join(Analysis, Analysis.review_id == Review.id)
        .outerjoin(Category, Category.id == Analysis.category_id)
        .where(Analysis.sentiment == sent_enum)
    )
    if src_ids:
        stmt = stmt.where(Review.source_id.in_(src_ids))
    if cat_filter is not None:
        stmt = stmt.where(Analysis.category_id.in_(cat_filter))
    stmt = stmt.order_by(Review.collected_at.desc()).limit(SAMPLE_SIZE)
    rows = (await session.execute(stmt)).all()

    if not rows:
        result = {
            "sentiment": sentiment,
            "categories": [],
            "themes": [],
            "sample_size": 0,
            "generated_at": time.time(),
            "message": "no_reviews_for_sentiment",
        }
        _set_cached(key, result)
        return result

    sample = []
    for rid, text, summary, cat_path in rows:
        snippet = (summary if summary else (text or "")).strip().replace("\n", " ")
        sample.append({
            "id": rid,
            "category": cat_path or UNCATEGORIZED_LABEL,
            "snippet": snippet[:400],
        })

    chosen_model = model or settings.ANTHROPIC_MODEL
    if chosen_model not in settings.allowed_models:
        chosen_model = settings.ANTHROPIC_MODEL

    try:
        parsed = await _call_claude(sample, sent_enum, summary_lang, chosen_model)
    except Exception as e:
        return {
            "sentiment": sentiment,
            "categories": [],
            "themes": [],
            "sample_size": len(sample),
            "error": str(e),
        }

    categories = parsed.get("categories") or []
    result = {
        "sentiment": sentiment,
        "categories": categories,
        "themes": _flatten(categories),  # legacy fallback
        "sample_size": len(sample),
        "model": chosen_model,
        "generated_at": time.time(),
    }
    _set_cached(key, result)
    return result
