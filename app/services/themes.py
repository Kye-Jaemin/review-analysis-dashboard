"""LLM-extracted reasons-behind-sentiment for the dashboard mind-map panel.

For a chosen sentiment band (e.g. very_positive) plus the current dashboard
filters, sample up to N matching reviews, send their summaries to Claude, and
ask for 5–8 themes with representative quotes. Results are cached in-process
for a few minutes per (sentiment, filter) signature so repeated dashboard
loads don't re-bill the Anthropic API.
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

_cache: dict[str, tuple[float, dict]] = {}


def _cache_key(sentiment: str, source_ids, root_ids, summary_lang: str) -> str:
    src = sorted(source_ids or [])
    rts = sorted(root_ids or [])
    raw = f"{sentiment}|{src}|{rts}|{summary_lang}"
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


async def _call_claude(sample, sentiment: Sentiment, summary_lang: str, model: str) -> list:
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    from anthropic import AsyncAnthropic

    lang_label = {"ko": "Korean", "en": "English"}.get(summary_lang, "English")

    system = (
        f"You analyze user reviews to surface the main themes behind a specific sentiment.\n"
        f"All reviews below share sentiment: {sentiment.value}\n\n"
        f"Identify 5–8 distinct themes that explain WHY users feel this way.\n"
        f"For each theme:\n"
        f"  - `theme`: short label in {lang_label}, 2–5 words\n"
        f"  - `count`: approximate integer of how many sampled reviews touch on this theme\n"
        f"  - `examples`: 2–3 SHORT (under 50 chars each) representative quote snippets,\n"
        f"    preserved in their original language\n\n"
        f"Respond with ONLY a JSON array. No prose, no markdown fences:\n"
        f'[\n'
        f'  {{"theme": "...", "count": 12, "examples": ["...", "..."]}},\n'
        f'  ...\n'
        f"]"
    )

    user_msg = "Reviews:\n" + "\n".join(
        f"[{r['id']}] {r['snippet']}" for r in sample
    )

    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    resp = await client.messages.create(
        model=model,
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(getattr(b, "text", "") for b in resp.content)
    text = _strip_fences(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"failed to parse LLM JSON: {e}; raw={text[:200]!r}")

    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        theme = str(item.get("theme") or "").strip()
        if not theme:
            continue
        count = item.get("count")
        try:
            count = int(count) if count is not None else None
        except (TypeError, ValueError):
            count = None
        examples_raw = item.get("examples") or []
        examples = []
        if isinstance(examples_raw, list):
            for e in examples_raw[:3]:
                if e is None:
                    continue
                examples.append(str(e).strip()[:80])
        out.append({"theme": theme[:80], "count": count, "examples": examples})
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
        return {"sentiment": sentiment, "themes": [], "sample_size": 0, "error": "invalid sentiment"}

    src_ids = _normalize_ids(source_ids)
    selected_roots = _normalize_ids(root_ids)

    cat_filter: Optional[set[int]] = None
    if selected_roots:
        all_cats = (await session.execute(select(Category))).scalars().all()
        parent_by_id = {c.id: c.parent_id for c in all_cats}
        cat_filter = _descendants_of(parent_by_id, selected_roots)

    stmt = (
        select(Review.id, Review.text, Analysis.summary)
        .join(Analysis, Analysis.review_id == Review.id)
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
            "themes": [],
            "sample_size": 0,
            "generated_at": time.time(),
            "message": "no_reviews_for_sentiment",
        }
        _set_cached(key, result)
        return result

    sample = []
    for rid, text, summary in rows:
        # Prefer the LLM's per-review summary; fall back to truncated text.
        snippet = (summary if summary else (text or "")).strip().replace("\n", " ")
        sample.append({"id": rid, "snippet": snippet[:400]})

    chosen_model = model or settings.ANTHROPIC_MODEL
    if chosen_model not in settings.allowed_models:
        chosen_model = settings.ANTHROPIC_MODEL

    try:
        themes = await _call_claude(sample, sent_enum, summary_lang, chosen_model)
    except Exception as e:
        return {
            "sentiment": sentiment,
            "themes": [],
            "sample_size": len(sample),
            "error": str(e),
        }

    result = {
        "sentiment": sentiment,
        "themes": themes,
        "sample_size": len(sample),
        "model": chosen_model,
        "generated_at": time.time(),
    }
    _set_cached(key, result)
    return result
