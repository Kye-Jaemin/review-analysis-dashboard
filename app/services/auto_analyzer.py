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
    the usual sentiment / score / confidence / summary.

    Analysis rows are upserted with sentiment / user_tier / summary —
    these are review-level attributes shared across cards. The
    per-card "this review belongs to this Top-10 category" link goes
    into the `review_auto_categories` junction table, so a review
    sitting in two cards keeps a tag for each.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import AsyncSessionLocal
from app.jobs import registry
from app.models import (
    Analysis,
    AnalysisJob,
    AnalysisStatus,
    AutoCategory,
    ReviewAutoCategoryLink,
    Review,
    Sentiment,
)
from app.models.analysis import SCORE_TO_SENTIMENT, SENTIMENT_TO_SCORE
from app.services.analyzer import _select_review_ids


def _link_upsert(values: list[dict]):
    """ON CONFLICT DO UPDATE insert for the auto junction, dialect-aware
    so the same call works on Postgres (prod) and SQLite (local/test).

    Updates the per-card sentiment snapshot (sentiment / sentiment_score /
    user_tier) when the same (review, auto_category) pair re-appears,
    so re-running auto analysis on this card refreshes its own snapshot
    without touching any sibling card's junction rows."""
    if not values:
        return None
    cols_to_refresh = {
        "sentiment": None,  # placeholder, replaced via stmt.excluded
        "sentiment_score": None,
        "user_tier": None,
    }
    if settings.database_url.startswith("postgresql"):
        stmt = pg_insert(ReviewAutoCategoryLink).values(values)
        return stmt.on_conflict_do_update(
            index_elements=["review_id", "auto_category_id"],
            set_={
                "sentiment": stmt.excluded.sentiment,
                "sentiment_score": stmt.excluded.sentiment_score,
                "user_tier": stmt.excluded.user_tier,
            },
        )
    stmt = sqlite_insert(ReviewAutoCategoryLink).values(values)
    return stmt.on_conflict_do_update(
        index_elements=["review_id", "auto_category_id"],
        set_={
            "sentiment": stmt.excluded.sentiment,
            "sentiment_score": stmt.excluded.sentiment_score,
            "user_tier": stmt.excluded.user_tier,
        },
    )

SAMPLE_SIZE_FOR_EXTRACTION = 200
TOP_N = 10

# Two fixed "simple sentiment" buckets that are always added alongside
# the LLM-derived Top 10 themes, giving a total of 12 categories per
# investigation. Their purpose: short generic praise like "great",
# "love it", "👍" or short generic complaints like "bad", "meh", "sucks"
# get a home of their own instead of bleeding into a topical theme.
# Position 10 / 11 (after the 10 themes) so existing UI rank ordering
# keeps content themes at the top of the doughnut.
SIMPLE_BUCKETS: dict[str, dict[str, dict[str, str]]] = {
    "en": {
        "simple_positive": {
            "name": "Simple praise",
            "description": (
                "Short generic positive reviews — single words or one-line "
                "compliments like 'great', 'love it', 'awesome', '👍' that "
                "don't tie to a specific feature or topic."
            ),
        },
        "simple_negative": {
            "name": "Simple complaint",
            "description": (
                "Short generic negative reviews — 'bad', 'sucks', 'meh', "
                "'don't like it' without a specific reason or topic."
            ),
        },
    },
    "ko": {
        "simple_positive": {
            "name": "단순 긍정",
            "description": (
                "특정 기능이나 주제 없이 '좋아요', '최고', '👍' 같은 짧은 "
                "일반 칭찬 한두 마디로 끝나는 리뷰."
            ),
        },
        "simple_negative": {
            "name": "단순 부정",
            "description": (
                "특정 이유 없이 '별로', '나빠요', '싫어요' 같은 짧은 "
                "일반 불만 한두 마디로 끝나는 리뷰."
            ),
        },
    },
}


def _simple_bucket_defs(summary_lang: str) -> list[dict]:
    """Return the two fixed simple-sentiment category dicts for a given
    UI language, in display order (simple_positive first, then
    simple_negative). Falls back to English if the lang isn't mapped."""
    table = SIMPLE_BUCKETS.get((summary_lang or "en").lower(), SIMPLE_BUCKETS["en"])
    return [
        {"name": table["simple_positive"]["name"],
         "description": table["simple_positive"]["description"]},
        {"name": table["simple_negative"]["name"],
         "description": table["simple_negative"]["description"]},
    ]


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
        f"  - useful for grouping reviews (appears in multiple)\n"
        f"  - SUBSTANTIVE — tied to a specific feature, behaviour, or topic.\n"
        f"    Do NOT propose a 'generic praise' / 'generic complaint' /\n"
        f"    'positive feedback' / 'negative feedback' theme. Two fixed\n"
        f"    buckets ('Simple praise' / 'Simple complaint') exist downstream\n"
        f"    for short generic reviews like 'great' or 'bad' that don't fit\n"
        f"    a content theme — your job here is purely to surface the real\n"
        f"    topics in the data, not to label sentiment.\n"
        f"  - SENTIMENT-NEUTRAL in the NAME — the category name must describe\n"
        f"    the TOPIC, not user reaction to it. A single topic bucket will\n"
        f"    collect BOTH positive and negative reviews about that topic, so\n"
        f"    a name that hard-codes one polarity (e.g. 'AI coaching aversion',\n"
        f"    'sleep tracking is inaccurate', 'transition complaints',\n"
        f"    'logging UX degradation', 'AI 코치 기능 거부감',\n"
        f"    '수면 추적 정확도 문제', '음식·칼로리 기록 기능 저하',\n"
        f"    '피트빗→구글헬스 전환 불만') becomes self-contradictory the\n"
        f"    moment a positive review lands in it.\n"
        f"      BAD  → 'AI 코치 기능 거부감'         (bakes negativity into name)\n"
        f"      GOOD → 'AI 코치 기능'                (topic only)\n"
        f"      BAD  → 'Sleep tracking inaccuracy'   (bakes negativity)\n"
        f"      GOOD → 'Sleep tracking accuracy'     (topic, neutral)\n"
        f"      BAD  → '피트빗→구글헬스 전환 불만'   (bakes negativity)\n"
        f"      GOOD → '피트빗→구글헬스 전환'        (topic only)\n"
        f"    The description sentence MAY note that opinions skew positive or\n"
        f"    negative, but the NAME stays neutral. Avoid evaluative words like\n"
        f"    complaint / aversion / inaccurate / degradation / problem / issue\n"
        f"    / praise / 불만 / 거부감 / 부정확 / 저하 / 문제 / 개선 / 결함\n"
        f"    in the name. Use the underlying noun instead (the feature, the\n"
        f"    behaviour, the workflow).\n\n"
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
        f"\n"
        f"Category routing — IMPORTANT:\n"
        f"  - The last two categories ('Simple praise' / 'Simple complaint',\n"
        f"    or '단순 긍정' / '단순 부정' in Korean) are reserved for SHORT\n"
        f"    GENERIC reviews that don't mention a specific feature or topic\n"
        f"    — things like 'great', 'awesome', 'love it', '👍', 'bad',\n"
        f"    'meh', 'sucks'. If a review has substantive content tied to a\n"
        f"    feature / topic, route it to one of the topical categories\n"
        f"    instead, even if it's also positive or negative.\n"
        f"  - In other words: positivity / negativity alone does NOT route\n"
        f"    to the simple buckets. Lack of substantive content does.\n"
        f"\n"
        f"Sentiment intensity calibration (IMPORTANT):\n"
        f"  - Reserve 'very_positive' / 'very_negative' for reviews with\n"
        f"    EXPLICIT strong language: superlatives ('absolutely amazing',\n"
        f"    'literally the best app ever', 'completely useless',\n"
        f"    'worst experience of my life'), multiple intensifiers, or\n"
        f"    strong emotional content (long enthusiastic praise / extended\n"
        f"    complaints).\n"
        f"  - Short generic praise like 'great', 'awesome', 'good', 'love it',\n"
        f"    '👍', 'nice app' — even with an exclamation mark — is plain\n"
        f"    'positive', NOT 'very_positive'.\n"
        f"  - Short generic complaint like 'bad', 'sucks', 'don't like it',\n"
        f"    'meh', 'disappointing' is plain 'negative', NOT 'very_negative'.\n"
        f"  - When the review is too short to convey real intensity (under ~6\n"
        f"    words and no superlatives), default to plain positive / negative\n"
        f"    / neutral.\n"
        f"  - Star ratings are a hint but not the rule: a 5★ review saying\n"
        f"    only 'good' is still plain 'positive'.\n"
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
            # IMPORTANT: sample from the FULL in-scope corpus, not just the
            # mode-selected ids. `_select_review_ids` orders by Review.id
            # ascending, so slicing `ids[:200]` always picked the earliest
            # collected reviews (mostly the first source). That made the
            # derived Top 10 a stale reflection of source #1 even after the
            # user added 1000 reviews from new sources. Randomising over
            # the whole scope gives every source a fair chance to surface
            # its themes.
            job.message = "extracting categories"
            scope_stmt = select(Review)
            if source_ids:
                scope_stmt = scope_stmt.where(Review.source_id.in_(source_ids))
            scope_stmt = scope_stmt.order_by(func.random()).limit(SAMPLE_SIZE_FOR_EXTRACTION)
            sample = (await session.execute(scope_stmt)).scalars().all()
            if not sample:
                # Fall back to mode-selected ids if the source filter yielded
                # nothing (shouldn't happen in practice, but defensive).
                sample = (
                    await session.execute(
                        select(Review).where(Review.id.in_(ids[:SAMPLE_SIZE_FOR_EXTRACTION]))
                    )
                ).scalars().all()
            cat_defs = await _extract_top_categories(sample, model, summary_lang)
            if not cat_defs:
                raise RuntimeError("LLM did not return any categories")
            # Append the two fixed "simple sentiment" buckets after the
            # LLM-derived 10 themes. Total = 12 categories per investigation.
            cat_defs = cat_defs + _simple_bucket_defs(summary_lang)

            # Replace any prior auto categories for this card. The junction
            # table has ON DELETE CASCADE on auto_category_id, so the deletes
            # also clear THIS card's tags for every affected review — other
            # cards' tags on the same reviews are untouched (their tags point
            # at their own auto_categories rows).
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
                    language=summary_lang or "en",
                    translations={},
                )
                session.add(ac)
                auto_cats.append(ac)
            await session.flush()
            # CRITICAL: commit Phase 1 so other sessions opened by Phase 2
            # batches can see the new auto_categories rows. Without this,
            # batch sessions try to insert/update Analysis rows whose
            # auto_category_id FK points to rows that aren't yet visible
            # to them, and Postgres raises ForeignKeyViolationError.
            # The same commit applies the SET-NULL cascade from the prior
            # delete, so other sessions also see the cleaned-up Analysis
            # rows in a consistent snapshot.
            await session.commit()
            # Capture the ids before the session can detach them.
            auto_cat_ids: list[int] = [ac.id for ac in auto_cats]
            auto_cats_by_id: dict[int, AutoCategory] = {ac.id: ac for ac in auto_cats}

            # ---- Phase 2: classify every in-scope review ----
            job.message = "classifying reviews"
            job.progress = 10

            batch_size = max(1, settings.ANALYSIS_BATCH_SIZE)
            concurrency = max(1, settings.ANALYSIS_CONCURRENCY)
            sem = asyncio.Semaphore(concurrency)

            processed = 0
            failed = 0
            per_cat_count: dict[int, int] = {cid: 0 for cid in auto_cat_ids}
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
                        link_rows: list[dict] = []  # junction inserts for this batch
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
                                        sentiment=sent,
                                        sentiment_score=score,
                                        confidence=conf,
                                        summary=item.get("summary"),
                                        user_tier=tier_val,
                                        model=model,
                                        status=AnalysisStatus.succeeded if success else AnalysisStatus.failed,
                                    )
                                )

                            # Per-card link goes into the junction. Card A's
                            # earlier tags on this review (pointing at Card A
                            # auto_categories) stay put — only this card's
                            # old tags were cleared by the cascade above.
                            #
                            # Sentiment snapshot lands on the junction too so
                            # this card's read path can show its own analysis
                            # of the review even after another card re-runs
                            # and overwrites the global Analysis row.
                            if ac_id_to_store is not None:
                                link_rows.append({
                                    "review_id": rid,
                                    "auto_category_id": ac_id_to_store,
                                    "sentiment": sent.value if sent is not None else None,
                                    "sentiment_score": score,
                                    "user_tier": tier_val,
                                })

                            if success:
                                processed += 1
                                if ac_id_to_store is not None:
                                    async with counts_lock:
                                        per_cat_count[ac_id_to_store] = per_cat_count.get(ac_id_to_store, 0) + 1
                            else:
                                failed += 1

                        # Any review in batch the LLM skipped → failed bucket.
                        # No junction insert for them (they aren't tagged on
                        # this card at all this run).
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
                                existing.status = AnalysisStatus.failed
                                existing.error = "no auto-classification output"
                                existing.analyzed_at = datetime.utcnow()
                            failed += 1

                        link_stmt = _link_upsert(link_rows)
                        if link_stmt is not None:
                            await s2.execute(link_stmt)
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
