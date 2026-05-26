from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import AsyncSessionLocal
from app.jobs import registry
from app.models import Analysis, AnalysisJob, AnalysisStatus, Category, Review, Sentiment
from app.models.analysis import SCORE_TO_SENTIMENT, SENTIMENT_TO_SCORE


@dataclass
class AnalyzerOutput:
    review_id: int
    category_path: Optional[str]
    sentiment: Optional[Sentiment]
    sentiment_score: Optional[int]
    confidence: Optional[float]
    summary: Optional[str]
    error: Optional[str] = None


def _serialize_tree(rows: list[Category]) -> str:
    by_id = {r.id: r for r in rows}
    children: dict[Optional[int], list[Category]] = {}
    for r in rows:
        children.setdefault(r.parent_id, []).append(r)

    lines: list[str] = []

    def walk(parent_id: Optional[int], depth: int):
        for node in sorted(children.get(parent_id, []), key=lambda x: x.name):
            indent = "  " * depth
            desc = f": {node.description}" if node.description else ""
            lines.append(f"{indent}- {node.name}{desc}")
            walk(node.id, depth + 1)

    walk(None, 0)
    if not lines:
        lines.append("- 기타 (Other): catch-all if user has not defined any category")
    return "\n".join(lines)


def _build_system_prompt(tree_text: str, summary_lang: str) -> str:
    lang_directive = {
        "en": "Write the `summary` field in English.",
        "ko": "summary 필드는 한국어로 작성하라.",
        "auto": "Write the `summary` field in the same language as the review.",
    }.get(summary_lang, "Write the `summary` field in English.")

    return f"""You are an analyst classifying user reviews. Below is a category tree the user defined.
Each leaf category has a description used as the classification rubric.

{tree_text}

For each review, assign:
- category_path: the leaf path joined with ' > '. If nothing fits, use '기타' or 'Other' if such a leaf exists; otherwise pick the closest leaf.
- sentiment: exactly one of [very_positive, positive, neutral, negative, very_negative]
- sentiment_score: integer 1..5 (1=very_negative, 5=very_positive). Must be consistent with sentiment.
- confidence: float in [0, 1]
- summary: one short sentence summarizing the review's point.

{lang_directive}

Respond with ONLY a JSON array of the same length and order as the inputs. No prose, no markdown fences."""


def _build_user_prompt(reviews: list[Review]) -> str:
    items = []
    for r in reviews:
        text = (r.text or "")[:1500]
        items.append({"id": r.id, "text": text, "rating": r.rating})
    return "Analyze these reviews:\n" + json.dumps(items, ensure_ascii=False)


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    return s.strip()


def _find_leaf_by_path(rows: list[Category], path: str) -> Optional[Category]:
    if not path:
        return None
    path_norm = " > ".join(p.strip() for p in path.replace(">", " > ").split(" > ") if p.strip())
    by_path = {r.path: r for r in rows}
    if path_norm in by_path:
        cand = by_path[path_norm]
        return cand
    # fuzzy: last segment match
    last = path_norm.split(" > ")[-1].lower()
    for r in rows:
        if r.name.lower() == last:
            return r
    return None


def _normalize(item: dict, reviews_by_id: dict[int, Review], categories: list[Category]) -> Optional[AnalyzerOutput]:
    rid = item.get("id")
    # Drop responses with a missing or hallucinated id; we can't safely attach
    # them to a Review (FK would fail) and the fallback loop in analyze_batch
    # will mark the corresponding real review as "no output for this review".
    if not isinstance(rid, int) or rid not in reviews_by_id:
        return None

    sentiment_raw = (item.get("sentiment") or "").strip().lower()
    try:
        sentiment = Sentiment(sentiment_raw)
    except ValueError:
        sentiment = None

    score_raw = item.get("sentiment_score")
    try:
        score = int(score_raw)
        if score < 1 or score > 5:
            score = None
    except (TypeError, ValueError):
        score = None

    # consistency: label wins
    if sentiment is not None:
        score = SENTIMENT_TO_SCORE[sentiment]
    elif score is not None:
        sentiment = SCORE_TO_SENTIMENT[score]

    conf_raw = item.get("confidence")
    try:
        confidence = float(conf_raw)
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = None

    summary = item.get("summary")
    if isinstance(summary, str):
        summary = summary.strip() or None

    category_path = item.get("category_path") or None
    return AnalyzerOutput(
        review_id=rid,
        category_path=category_path,
        sentiment=sentiment,
        sentiment_score=score,
        confidence=confidence,
        summary=summary,
    )


async def _call_claude(model: str, system: str, user: str) -> list[dict]:
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    resp = await client.messages.create(
        model=model,
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(getattr(b, "text", "") for b in resp.content)
    text = _strip_fences(text)
    data = json.loads(text)
    if isinstance(data, dict):
        data = [data]
    return data


async def analyze_batch(
    model: str,
    summary_lang: str,
    reviews: list[Review],
    categories: list[Category],
) -> list[AnalyzerOutput]:
    if not reviews:
        return []
    system = _build_system_prompt(_serialize_tree(categories), summary_lang)
    user = _build_user_prompt(reviews)
    reviews_by_id = {r.id: r for r in reviews}
    try:
        raw = await _call_claude(model, system, user)
    except Exception as e:
        return [
            AnalyzerOutput(
                review_id=r.id, category_path=None, sentiment=None,
                sentiment_score=None, confidence=None, summary=None,
                error=str(e),
            )
            for r in reviews
        ]
    outputs: list[AnalyzerOutput] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        norm = _normalize(item, reviews_by_id, categories)
        if norm is not None:
            outputs.append(norm)
    seen = {o.review_id for o in outputs}
    for r in reviews:
        if r.id not in seen:
            outputs.append(AnalyzerOutput(
                review_id=r.id, category_path=None, sentiment=None,
                sentiment_score=None, confidence=None, summary=None,
                error="no output for this review",
            ))
    return outputs


async def _select_review_ids(session: AsyncSession, mode: str) -> list[int]:
    from sqlalchemy import and_, or_

    base = select(Review.id).outerjoin(Analysis, Analysis.review_id == Review.id)
    if mode == "unanalyzed":
        stmt = base.where(Analysis.id.is_(None))
    elif mode == "failed":
        stmt = base.where(Analysis.status == AnalysisStatus.failed)
    elif mode == "all":
        stmt = select(Review.id)
    else:
        stmt = base.where(Analysis.id.is_(None))
    result = await session.execute(stmt.order_by(Review.id))
    return [row[0] for row in result.all()]


async def run_analysis_job(
    job_id: str,
    db_job_id: int,
    mode: str,
    model: str,
    summary_lang: str,
) -> None:
    job = registry.get(job_id)
    if not job:
        return
    job.status = "running"

    try:
        async with AsyncSessionLocal() as session:
            cat_rows = (await session.execute(select(Category))).scalars().all()
            ids = await _select_review_ids(session, mode)
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

            batch_size = max(1, settings.ANALYSIS_BATCH_SIZE)
            concurrency = max(1, settings.ANALYSIS_CONCURRENCY)
            sem = asyncio.Semaphore(concurrency)

            processed = 0
            failed = 0

            async def _process_batch(batch_ids: list[int]):
                nonlocal processed, failed
                async with sem:
                    async with AsyncSessionLocal() as s2:
                        rows = (await s2.execute(select(Review).where(Review.id.in_(batch_ids)))).scalars().all()
                        outs = await analyze_batch(model, summary_lang, rows, cat_rows)
                        valid_ids = {r.id for r in rows}
                        for out in outs:
                            # Defensive: never try to insert against a review_id
                            # that isn't in this batch — would FK-violate.
                            if out.review_id not in valid_ids:
                                continue
                            existing = (
                                await s2.execute(select(Analysis).where(Analysis.review_id == out.review_id))
                            ).scalar_one_or_none()
                            cat = _find_leaf_by_path(cat_rows, out.category_path or "") if out.category_path else None
                            success = out.error is None and out.sentiment is not None
                            if existing:
                                existing.category_id = cat.id if cat else None
                                existing.sentiment = out.sentiment
                                existing.sentiment_score = out.sentiment_score
                                existing.confidence = out.confidence
                                existing.summary = out.summary
                                existing.model = model
                                existing.status = AnalysisStatus.succeeded if success else AnalysisStatus.failed
                                existing.error = out.error
                                existing.analyzed_at = datetime.utcnow()
                            else:
                                s2.add(Analysis(
                                    review_id=out.review_id,
                                    category_id=cat.id if cat else None,
                                    sentiment=out.sentiment,
                                    sentiment_score=out.sentiment_score,
                                    confidence=out.confidence,
                                    summary=out.summary,
                                    model=model,
                                    status=AnalysisStatus.succeeded if success else AnalysisStatus.failed,
                                    error=out.error,
                                ))
                            if success:
                                processed += 1
                            else:
                                failed += 1
                        await s2.commit()
                job.processed = processed
                job.failed_count = failed
                done = processed + failed
                job.progress = min(99, int(done / max(job.total, 1) * 100))
                job.message = f"ok {processed}, failed {failed}"

            batches = [ids[i : i + batch_size] for i in range(0, len(ids), batch_size)]
            await asyncio.gather(*[_process_batch(b) for b in batches])

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
