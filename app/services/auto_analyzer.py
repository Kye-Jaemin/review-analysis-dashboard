"""Auto-category analysis.

Two-pass LLM flow scoped to an Investigation card:

  Phase 1 — derive the Top-N categories directly from the data.
    Sample a slice of in-scope reviews, ask Claude to read them and emit
    distinct, recognisable categories (name + 1-line description).
    Persist as AutoCategory rows attached to the card, replacing whatever
    set was there before.

  Phase 2 — classify every in-scope review into one of those categories.
    Batched call: each batch includes the 10 categories + a chunk of
    reviews; the response tags every review with a category_index plus
    the usual sentiment / score / confidence / summary. Analysis rows
    are upserted with auto_category_id set; manual category_id is left
    alone (so a card can carry both classifications without losing
    either).
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Optional

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import AsyncSessionLocal
from app.jobs import registry
from app.models import (
    Analysis,
    AnalysisJob,
    AnalysisStatus,
    AutoCategory,
    Review,
    Sentiment,
)
from app.models.analysis import SCORE_TO_SENTIMENT, SENTIMENT_TO_SCORE
from app.services.analyzer import _select_review_ids

SAMPLE_SIZE_FOR_EXTRACTION = 200
TOP_N = 10


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    return s.strip()


def _lang_label(summary_lang: str) -> str:
    return {"ko": "Korean", "en": "English"}.get(summary_lang, "English")


async def _extract_top_categories(
    sample_reviews: list[Review], model: str, summary_lang: str
) -> list[dict]:
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    from anthropic import AsyncAnthropic

    lang_label = _lang_label(summary_lang)
    system = (
        f"You analyze user reviews to derive natural categories from the data.\n"
        f"Read the reviews and identify the TOP {TOP_N} most prominent, "
        f"distinct themes / topics that emerge from THIS specific dataset.\n"
        f"Each category should be:\n"
        f"  - recognisable from real review content (don't invent generic buckets)\n"
        f"  - distinct from the others (minimal overlap)\n"
        f"  - useful for grouping reviews (appears in multiple)\n\n"
        f"Output JSON only:\n"
        f"{{\n"
        f'  "categories": [\n'
        f'    {{"name": "<short 3–5 word label in {lang_label}>",\n'
        f'      "description": "<one-sentence rubric in {lang_label}>"}},\n'
        f"    ...\n"
        f"  ]\n"
        f"}}\n"
        f"Exactly {TOP_N} entries. No prose, no markdown fences."
    )
    user = "Reviews:\n" + "\n".join(
        f"[{r.id}] {((r.text or '').strip().replace(chr(10), ' '))[:300]}"
        for r in sample_reviews
    )

    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    resp = await client.messages.create(
        model=model, max_tokens=2048, system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = _strip_fences("".join(getattr(b, "text", "") for b in resp.content))
    parsed = json.loads(text)
    cats = parsed.get("categories") or []
    out = []
    for c in cats[:TOP_N]:
        if not isinstance(c, dict):
            continue
        name = (c.get("name") or "").strip()
        if not name:
            continue
        out.append({"name": name[:200], "description": (c.get("description") or "").strip()[:500] or None})
    return out


async def _classify_batch(
    categories: list[AutoCategory],
    reviews: list[Review],
    model: str,
    summary_lang: str,
    separate_user_tier: bool = False,
) -> list[dict]:
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    from anthropic import AsyncAnthropic

    lang_label = _lang_label(summary_lang)
    cat_block = "\n".join(
        f"{i}. {c.name} — {c.description or ''}" for i, c in enumerate(categories)
    )

    tier_field = ""
    tier_instruction = ""
    if separate_user_tier:
        tier_field = (
            '   "user_tier": "paid"|"free"|"unknown",\n'
        )
        tier_instruction = (
            "\nAlso infer user_tier from review content:\n"
            "  - paid: mentions Premium / subscription / paid features / they\n"
            "    explicitly already paid (e.g. 'I subscribed', 'as a Premium user').\n"
            "  - free: mentions ads, the free tier limitations, upgrade prompts,\n"
            "    'free version', 'won't pay', etc.\n"
            "  - unknown: no clear signal; do NOT guess from sentiment alone.\n"
            "(BETA — uncertain values should fall back to 'unknown'.)\n"
        )

    system = (
        f"Below are exactly {len(categories)} categories.\n\n"
        f"{cat_block}\n\n"
        f"For each review, pick the index (0..{len(categories)-1}) of the BEST fitting\n"
        f"category. Also rate sentiment + confidence.\n"
        f"{tier_instruction}\n"
        f"For each review respond with:\n"
        f'  {{"id": <input id>,\n'
        f'   "category_index": <int 0..{len(categories)-1}>,\n'
        f'   "sentiment": "very_positive"|"positive"|"neutral"|"negative"|"very_negative",\n'
        f'   "sentiment_score": <1..5 consistent with sentiment>,\n'
        f'   "confidence": <0..1>,\n'
        f"{tier_field}"
        f'   "summary": "<one short sentence in {lang_label}>"}}\n\n'
        f"Reply with ONLY a JSON array, no prose, no fences."
    )
    items = [
        {"id": r.id, "text": (r.text or "")[:1500], "rating": r.rating}
        for r in reviews
    ]
    user = "Reviews:\n" + json.dumps(items, ensure_ascii=False)

    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    resp = await client.messages.create(
        model=model, max_tokens=2048, system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = _strip_fences("".join(getattr(b, "text", "") for b in resp.content))
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


async def run_auto_analysis_job(
    job_id: str,
    db_job_id: int,
    investigation_id: int,
    mode: str,
    model: str,
    summary_lang: str,
    source_ids: list[int] | None = None,
    min_confidence: float = 0.0,
    separate_user_tier: bool = False,
) -> None:
    job = registry.get(job_id)
    if not job:
        return
    job.status = "running"

    try:
        async with AsyncSessionLocal() as session:
            ids = await _select_review_ids(session, mode, source_ids=source_ids)
            job.total = len(ids)
            if not ids:
                job.status = "succeeded"
                job.message = "no reviews to analyze"
                job.progress = 100
                job.finished_at = datetime.utcnow()
                aj = await session.get(AnalysisJob, db_job_id)
                if aj:
                    aj.status = "succeeded"
                    aj.finished_at = datetime.utcnow()
                    await session.commit()
                return

            # ---- Phase 1: extract TopN categories from a sample ----
            job.message = "extracting categories"
            sample_ids = ids[: SAMPLE_SIZE_FOR_EXTRACTION]
            sample = (
                await session.execute(select(Review).where(Review.id.in_(sample_ids)))
            ).scalars().all()
            cat_defs = await _extract_top_categories(sample, model, summary_lang)
            if not cat_defs:
                raise RuntimeError("LLM did not return any categories")

            # Replace any prior auto categories for this card. Analysis rows
            # have ON DELETE SET NULL, so their auto_category_id resets cleanly.
            await session.execute(
                delete(AutoCategory).where(AutoCategory.investigation_id == investigation_id)
            )
            await session.flush()

            auto_cats: list[AutoCategory] = []
            for idx, c in enumerate(cat_defs):
                ac = AutoCategory(
                    investigation_id=investigation_id,
                    name=c["name"],
                    description=c.get("description"),
                    review_count=0,
                    display_order=idx,
                )
                session.add(ac)
                auto_cats.append(ac)
            await session.flush()

            # ---- Phase 2: classify every in-scope review ----
            job.message = "classifying reviews"
            job.progress = 10

            batch_size = max(1, settings.ANALYSIS_BATCH_SIZE)
            concurrency = max(1, settings.ANALYSIS_CONCURRENCY)
            sem = asyncio.Semaphore(concurrency)

            processed = 0
            failed = 0
            per_cat_count: dict[int, int] = {ac.id: 0 for ac in auto_cats}
            counts_lock = asyncio.Lock()

            async def _process_batch(batch_ids: list[int]):
                nonlocal processed, failed
                async with sem:
                    async with AsyncSessionLocal() as s2:
                        rows = (
                            await s2.execute(select(Review).where(Review.id.in_(batch_ids)))
                        ).scalars().all()
                        try:
                            outs = await _classify_batch(
                                auto_cats, rows, model, summary_lang,
                                separate_user_tier=separate_user_tier,
                            )
                        except Exception:
                            outs = []
                        valid_ids = {r.id for r in rows}
                        seen: set[int] = set()
                        for item in outs:
                            if not isinstance(item, dict):
                                continue
                            rid = item.get("id")
                            if rid not in valid_ids:
                                continue
                            seen.add(rid)
                            idx = item.get("category_index")
                            if isinstance(idx, int) and 0 <= idx < len(auto_cats):
                                auto_cat = auto_cats[idx]
                            else:
                                auto_cat = None

                            try:
                                sent = Sentiment(item.get("sentiment"))
                                score = SENTIMENT_TO_SCORE[sent]
                            except (ValueError, KeyError, TypeError):
                                sent = None
                                score = None
                            try:
                                conf = float(item.get("confidence"))
                                conf = max(0.0, min(1.0, conf))
                            except (TypeError, ValueError):
                                conf = None

                            ac_id_to_store = auto_cat.id if auto_cat else None
                            if (
                                ac_id_to_store is not None
                                and min_confidence > 0
                                and conf is not None
                                and conf < min_confidence
                            ):
                                ac_id_to_store = None

                            tier_val = None
                            if separate_user_tier:
                                tier_raw = item.get("user_tier")
                                if isinstance(tier_raw, str):
                                    tier_norm = tier_raw.strip().lower()
                                    if tier_norm in ("paid", "free", "unknown"):
                                        tier_val = tier_norm
                                if tier_val is None:
                                    tier_val = "unknown"

                            existing = (
                                await s2.execute(
                                    select(Analysis).where(Analysis.review_id == rid)
                                )
                            ).scalar_one_or_none()
                            success = sent is not None
                            if existing:
                                existing.auto_category_id = ac_id_to_store
                                if sent is not None:
                                    existing.sentiment = sent
                                    existing.sentiment_score = score
                                if conf is not None:
                                    existing.confidence = conf
                                if item.get("summary"):
                                    existing.summary = item.get("summary")
                                if separate_user_tier:
                                    existing.user_tier = tier_val
                                existing.model = model
                                existing.status = (
                                    AnalysisStatus.succeeded if success else AnalysisStatus.failed
                                )
                                existing.analyzed_at = datetime.utcnow()
                            else:
                                s2.add(
                                    Analysis(
                                        review_id=rid,
                                        auto_category_id=ac_id_to_store,
                                        sentiment=sent,
                                        sentiment_score=score,
                                        confidence=conf,
                                        summary=item.get("summary"),
                                        user_tier=tier_val,
                                        model=model,
                                        status=AnalysisStatus.succeeded if success else AnalysisStatus.failed,
                                    )
                                )
                            if success:
                                processed += 1
                                if ac_id_to_store is not None:
                                    async with counts_lock:
                                        per_cat_count[ac_id_to_store] = per_cat_count.get(ac_id_to_store, 0) + 1
                            else:
                                failed += 1

                        # Any review in batch the LLM skipped → failed bucket
                        for missed in valid_ids - seen:
                            existing = (
                                await s2.execute(
                                    select(Analysis).where(Analysis.review_id == missed)
                                )
                            ).scalar_one_or_none()
                            if existing is None:
                                s2.add(
                                    Analysis(
                                        review_id=missed,
                                        status=AnalysisStatus.failed,
                                        error="no auto-classification output",
                                        model=model,
                                    )
                                )
                            else:
                                existing.auto_category_id = None
                                existing.status = AnalysisStatus.failed
                                existing.error = "no auto-classification output"
                                existing.analyzed_at = datetime.utcnow()
                            failed += 1

                        await s2.commit()
                job.processed = processed
                job.failed_count = failed
                done = processed + failed
                job.progress = min(99, 10 + int(done / max(job.total, 1) * 89))
                job.message = f"classify: ok {processed}, failed {failed}"

            batches = [ids[i : i + batch_size] for i in range(0, len(ids), batch_size)]
            await asyncio.gather(*[_process_batch(b) for b in batches])

            # Write per-category review_count cache.
            for ac in auto_cats:
                await session.execute(
                    update(AutoCategory)
                    .where(AutoCategory.id == ac.id)
                    .values(review_count=per_cat_count.get(ac.id, 0))
                )

            aj = await session.get(AnalysisJob, db_job_id)
            if aj:
                aj.status = "succeeded"
                aj.finished_at = datetime.utcnow()
                aj.processed_count = processed
                aj.failed_count = failed
                aj.model = model
            await session.commit()

        job.status = "succeeded"
        job.progress = 100
        job.finished_at = datetime.utcnow()
    except Exception as e:
        job.status = "failed"
        job.error = str(e)
        job.finished_at = datetime.utcnow()
        try:
            async with AsyncSessionLocal() as session:
                aj = await session.get(AnalysisJob, db_job_id)
                if aj:
                    aj.status = "failed"
                    aj.error = str(e)[:2000]
                    aj.finished_at = datetime.utcnow()
                    await session.commit()
        except Exception:
            pass
