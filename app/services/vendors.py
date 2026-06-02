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

import math
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

# 95 % confidence z-score for the Wilson interval. Anything in the
# 90 – 99 % range works; 95 % is the conventional default and what
# Reddit / Amazon use for their "best of" rankings.
_WILSON_Z = 1.96


def _wilson_lower_bound(p_hat: float, n: int, z: float = _WILSON_Z) -> float:
    """Lower bound of the Wilson score interval for a binomial proportion.

    Given an observed positive ratio p_hat out of n samples, returns the
    LOWER edge of the 95 % confidence interval for the true ratio. This
    is what we sort by instead of the raw p_hat so that:

      - 15 reviews × 100 % positive  →  Wilson ≈ 0.80
      - 500 reviews × 85 % positive  →  Wilson ≈ 0.82  (wins, correctly)

    The raw p_hat for the smaller sample is higher but our confidence
    that the true rate is that high is weaker — Wilson encodes that.
    Reddit's "Best" comment ranking is the canonical reference.
    """
    if n <= 0:
        return 0.0
    p_hat = max(0.0, min(1.0, p_hat))
    denom = 1.0 + (z * z) / n
    centre = p_hat + (z * z) / (2.0 * n)
    margin = z * math.sqrt((p_hat * (1.0 - p_hat) + (z * z) / (4.0 * n)) / n)
    return max(0.0, (centre - margin) / denom)

# Vendor stems that are actually the same company under different store
# branding. After _vendor_key extracts the source's stem, this map merges
# variants down to a single canonical key so the dashboard groups them
# under one row. Keys are post-stem lowercased forms; values are the
# canonical group key. Both directions matter — a stem that already
# matches a value stays as-is.
_VENDOR_ALIASES: dict[str, str] = {
    # Google rebranded Fitbit's app while leaving the standalone Fitbit
    # store listings alive — same company.
    "google health (fitbit)": "fitbit",
    "google health fitbit": "fitbit",
    "google health": "fitbit",
    # WeightWatchers / Weight Watchers Program / weightwatchers — same
    # brand, varying spaces / formality across stores.
    "weight watchers program": "weightwatchers",
    "weight watchers": "weightwatchers",
    "weightwatchers program": "weightwatchers",
    "ww": "weightwatchers",  # the rebrand name people use sometimes
    # Apple Fitness+ is sometimes "AppleFitnessPlus" (no space) and
    # sometimes "Apple Fitness" in the App Store listing.
    "applefitnessplus": "apple fitness",
    "apple fitness+": "apple fitness",
    "apple fitness plus": "apple fitness",
}

# Auto-category names that aren't real "strengths" or "weaknesses" but
# are the bulk sentiment buckets the analyzer always inserts (Top 10 + 2
# fixed simple buckets). Excluded from the strength/weakness ranking so
# they don't dominate the lists with 100%-positive or 100%-negative
# trivial entries. Matched case-insensitively against the lowercased
# auto_category.name, in both en + ko.
_SIMPLE_BUCKET_NAMES = {
    # English (SIMPLE_BUCKETS in auto_analyzer.py)
    "simple praise",
    "simple complaint",
    # Korean
    "단순 긍정",
    "단순 부정",
}


def _vendor_key(display_name: Optional[str], label: Optional[str]) -> str:
    """Reduce a source's display name to its vendor stem.

    Heuristics (in order):
      - drop a leading 'r/' so reddit subreddits group with the app
      - cut at the first ':' or ' - ' to drop subtitles like
        ':AI Calorie Counter' / '- Food Tracker'
      - lowercase + strip
      - apply _VENDOR_ALIASES to merge known brand variants
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
    stem = name.strip().lower()
    # Brand alias merge — e.g. "google health (fitbit)" → "fitbit".
    return _VENDOR_ALIASES.get(stem, stem)


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
        # Find every investigation card that targets THIS vendor specifically.
        # Two filters guard against attributing other vendors' themes:
        #
        #  1. Skip hidden cards — the user has explicitly removed them from
        #     the analysis surface and they shouldn't leak back in here.
        #  2. Skip multi-vendor cards (e.g. a card combining 8 fitness apps)
        #     because their auto-categories like "운동·심박존 커스터마이징
        #     부재" are extracted from the COMBINED corpus and reflect
        #     themes across multiple brands. Attributing those categories
        #     wholesale to each overlapping vendor causes cross-brand
        #     leakage. Only include cards whose source_ids are entirely
        #     contained in this vendor's source pool (a "pure" vendor card).
        #
        # source_ids is a JSON column so we filter in Python.
        all_invs = (await session.execute(select(Investigation))).scalars().all()
        vendor_src_set = set(src_ids)
        relevant_inv_ids: set[int] = set()
        for inv in all_invs:
            if getattr(inv, "hidden", False):
                continue
            inv_srcs = set(inv.source_ids or [])
            if not inv_srcs:
                continue
            # Strict containment: every source in the card belongs to this
            # vendor. Eliminates multi-vendor cards.
            if inv_srcs <= vendor_src_set:
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
            #
            # The two fixed simple-sentiment buckets ("Simple praise" /
            # "Simple complaint" / "단순 긍정" / "단순 부정") are skipped
            # here so they don't bubble to the top of strengths /
            # weaknesses as 100%-positive / 100%-negative trivial wins —
            # they aren't features the vendor is good or bad AT, they're
            # just sentiment dumps. The dashboard's auto-cat doughnut
            # still shows them; only the vendor strength/weakness
            # ranking excludes them.
            by_name: dict[str, dict] = {}
            for c in cats:
                nm = (c.name or "").strip()
                if not nm:
                    continue
                if nm.lower() in _SIMPLE_BUCKET_NAMES:
                    continue
                key_n = nm.lower()
                node = by_name.setdefault(
                    key_n,
                    {
                        "name": nm,
                        "description": c.description,
                        "sentiments": {b: 0 for b in SENTIMENT_ORDER},
                        "total": 0,
                        # Every AutoCategory row that fed this by_name entry.
                        # Downstream callers (e.g. the competitive-rank
                        # service) use this list to look up sample reviews
                        # via the junction table.
                        "cat_ids": [],
                    },
                )
                node["cat_ids"].append(c.id)
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
                # Wilson lower bounds — the actual ranking key. The raw
                # pos_pct / neg_pct stays on the response so the UI still
                # shows the observed ratio the user expects to see; sort
                # order is driven by the confidence-adjusted score so
                # 15-review wins don't outrank 500-review wins.
                node["pos_score"] = _wilson_lower_bound(node["pos_pct"], total)
                node["neg_score"] = _wilson_lower_bound(node["neg_pct"], total)
                # A "small sample" flag for the UI so the dashboard can mark
                # entries the user should treat with caution (still shown,
                # just visually de-emphasised or hinted).
                node["small_sample"] = total < 30
                cat_buckets.append(node)

        strengths = sorted(
            cat_buckets, key=lambda x: (x["pos_score"], x["total"]), reverse=True
        )[:5]
        weaknesses = sorted(
            cat_buckets, key=lambda x: (x["neg_score"], x["total"]), reverse=True
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
