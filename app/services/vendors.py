"""Vendor analysis aggregation.

Groups sources by a derived "vendor key" (the leading app/brand name
stripped of store-specific suffixes and Reddit's r/ prefix), then rolls
up every analysed review under that vendor to:

  - 5-band sentiment distribution
  - average sentiment score (1..5)
  - average rating (where applicable)
  - top strengths: auto categories with the highest positive ratio
  - top weaknesses: auto categories with the highest negative ratio

Pure read-only over existing data — no new LLM calls. Auto categories
are aggregated across every investigation card that targets the
vendor's sources, so a vendor's "강점/약점" picture reflects the union
of all the Top-10 buckets the user has built up.
"""
from __future__ import annotations

import re
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Analysis,
    AutoCategory,
    Investigation,
    ReviewAutoCategoryLink,
    Review,
    Source,
)


# Sentiment buckets ordered worst → best so callers can iterate in display
# order without re-sorting.
SENTIMENT_ORDER = [
    "very_negative", "negative", "neutral", "positive", "very_positive",
]
_POS_BAND = {"positive", "very_positive"}
_NEG_BAND = {"negative", "very_negative"}
_SCORE = {
    "very_negative": 1,
    "negative": 2,
    "neutral": 3,
    "positive": 4,
    "very_positive": 5,
}

# Categories must have at least this many reviews to be considered a
# strength or weakness — single-review categories with 100% positive
# would otherwise dominate the rankings.
_MIN_BAND_REVIEWS = 10


def _vendor_key(display_name: Optional[str], label: Optional[str]) -> str:
    """Reduce a source's display name to its vendor stem.

    Heuristics (in order):
      - drop a leading 'r/' so reddit subreddits group with the app
      - cut at the first ':' or ' - ' to drop subtitles like
        ':AI Calorie Counter' / '- Food Tracker'
      - lowercase + strip
    """
    name = (display_name or label or "").strip()
    if not name:
        return ""
    if name.lower().startswith("r/"):
        name = name[2:]
    # ':' (BitePal: AI Calorie Counter) and ' - ' (Cal AI - Food Tracker)
    for sep in (":", " - ", " – "):
        if sep in name:
            name = name.split(sep, 1)[0]
            break
    return name.strip().lower()


async def list_vendors(session: AsyncSession) -> list[dict]:
    """Return every vendor in the workspace with a roll-up of sentiment
    plus the top strengths / weaknesses."""
    sources = (await session.execute(select(Source))).scalars().all()
    if not sources:
        return []

    # vendor_key → {sources, display_label, source_ids}
    vendor_groups: dict[str, dict] = {}
    for s in sources:
        key = _vendor_key(s.display_name, s.label)
        if not key:
            continue
        g = vendor_groups.setdefault(
            key,
            {
                "key": key,
                # The display label is whatever the first source's
                # display_name says, with r/ stripped + the suffix
                # we trimmed for the key. Pick the longest non-empty
                # original as the friendlier title.
                "display": "",
                "source_ids": [],
                "platforms": set(),
                "icon_url": None,
            },
        )
        g["source_ids"].append(s.id)
        plat = s.type.value if hasattr(s.type, "value") else str(s.type)
        g["platforms"].add(plat)
        if s.icon_url and not g["icon_url"]:
            g["icon_url"] = s.icon_url
        candidate = (s.display_name or s.label or "").strip()
        if candidate.lower().startswith("r/"):
            candidate = candidate[2:]
        if len(candidate) > len(g["display"]):
            g["display"] = candidate

    vendors: list[dict] = []
    for key, g in vendor_groups.items():
        src_ids = g["source_ids"]
        if not src_ids:
            continue

        # ---- Total reviews + per-band sentiment for this vendor ----
        total_reviews = (
            await session.execute(
                select(func.count(Review.id)).where(Review.source_id.in_(src_ids))
            )
        ).scalar() or 0
        if not total_reviews:
            continue

        sentiment_counts = {b: 0 for b in SENTIMENT_ORDER}
        rows = (
            await session.execute(
                select(Analysis.sentiment, func.count(Analysis.id))
                .join(Review, Review.id == Analysis.review_id)
                .where(Review.source_id.in_(src_ids))
                .group_by(Analysis.sentiment)
            )
        ).all()
        analyzed_total = 0
        for sent, count in rows:
            if sent is None:
                continue
            s_key = sent.value if hasattr(sent, "value") else str(sent)
            if s_key in sentiment_counts:
                sentiment_counts[s_key] += int(count)
                analyzed_total += int(count)

        avg_rating = (
            await session.execute(
                select(func.avg(Review.rating)).where(Review.source_id.in_(src_ids))
            )
        ).scalar()
        avg_sent_score = None
        if analyzed_total > 0:
            weighted = sum(_SCORE[b] * sentiment_counts[b] for b in SENTIMENT_ORDER)
            avg_sent_score = weighted / analyzed_total

        # ---- Strengths / weaknesses from auto categories ----
        # Find every investigation card that targets any of this vendor's
        # sources; their auto categories form the candidate pool.
        # source_ids is a JSON column so we filter in Python.
        all_invs = (await session.execute(select(Investigation))).scalars().all()
        relevant_inv_ids: set[int] = set()
        for inv in all_invs:
            inv_srcs = set(inv.source_ids or [])
            if inv_srcs & set(src_ids):
                relevant_inv_ids.add(inv.id)
        cats: list[AutoCategory] = []
        if relevant_inv_ids:
            cats = (
                await session.execute(
                    select(AutoCategory).where(
                        AutoCategory.investigation_id.in_(relevant_inv_ids)
                    )
                )
            ).scalars().all()

        # For each auto-cat: count reviews per sentiment band, scoped to
        # this vendor's sources.
        cat_buckets: list[dict] = []
        if cats:
            cat_ids = [c.id for c in cats]
            link_rows = (
                await session.execute(
                    select(
                        ReviewAutoCategoryLink.c.auto_category_id,
                        Analysis.sentiment,
                        func.count(Analysis.id),
                    )
                    .select_from(ReviewAutoCategoryLink)
                    .join(Analysis, Analysis.review_id == ReviewAutoCategoryLink.c.review_id)
                    .join(Review, Review.id == Analysis.review_id)
                    .where(ReviewAutoCategoryLink.c.auto_category_id.in_(cat_ids))
                    .where(Review.source_id.in_(src_ids))
                    .group_by(
                        ReviewAutoCategoryLink.c.auto_category_id,
                        Analysis.sentiment,
                    )
                )
            ).all()
            per_cat: dict[int, dict[str, int]] = {
                cid: {b: 0 for b in SENTIMENT_ORDER} for cid in cat_ids
            }
            for cid, sent, count in link_rows:
                if sent is None:
                    continue
                s_key = sent.value if hasattr(sent, "value") else str(sent)
                if s_key in per_cat.get(cid, {}):
                    per_cat[cid][s_key] += int(count)

            # Dedup auto-cats that appear with the same name across
            # multiple investigations (same vendor, two cards). Sum their
            # counts. Use lowercased name as the dedupe key.
            by_name: dict[str, dict] = {}
            for c in cats:
                nm = (c.name or "").strip()
                if not nm:
                    continue
                key_n = nm.lower()
                node = by_name.setdefault(
                    key_n,
                    {
                        "name": nm,
                        "description": c.description,
                        "sentiments": {b: 0 for b in SENTIMENT_ORDER},
                        "total": 0,
                    },
                )
                src_counts = per_cat.get(c.id, {})
                for band, cnt in src_counts.items():
                    node["sentiments"][band] += cnt
                    node["total"] += cnt

            for node in by_name.values():
                total = node["total"]
                if total < _MIN_BAND_REVIEWS:
                    continue
                pos = sum(node["sentiments"][b] for b in _POS_BAND)
                neg = sum(node["sentiments"][b] for b in _NEG_BAND)
                node["pos_pct"] = pos / total
                node["neg_pct"] = neg / total
                cat_buckets.append(node)

        strengths = sorted(
            cat_buckets, key=lambda x: (x["pos_pct"], x["total"]), reverse=True
        )[:5]
        weaknesses = sorted(
            cat_buckets, key=lambda x: (x["neg_pct"], x["total"]), reverse=True
        )[:5]

        vendors.append({
            "key": key,
            "display": g["display"] or key,
            "platforms": sorted(g["platforms"]),
            "icon_url": g["icon_url"],
            "source_ids": sorted(src_ids),
            "review_count": int(total_reviews),
            "analyzed_count": int(analyzed_total),
            "sentiments": sentiment_counts,
            "avg_sentiment_score": (
                round(avg_sent_score, 2) if avg_sent_score is not None else None
            ),
            "avg_rating": (
                round(float(avg_rating), 2) if avg_rating is not None else None
            ),
            "strengths": strengths,
            "weaknesses": weaknesses,
        })

    # Sort vendors by analyzed review count descending — the user's biggest
    # bodies of feedback float to the top.
    vendors.sort(key=lambda v: v["analyzed_count"], reverse=True)
    return vendors
