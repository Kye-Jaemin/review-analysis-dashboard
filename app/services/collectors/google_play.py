from __future__ import annotations

import asyncio
from datetime import datetime
from typing import AsyncIterator

from app.services.collectors.base import CollectedItem, CollectorBase, json_safe


class GooglePlayCollector(CollectorBase):
    @classmethod
    async def search(cls, query: str, country: str = "us", lang: str = "en", **kwargs) -> list[dict]:
        from google_play_scraper import search as gp_search

        # Bump n_hits so developer-name matches (which the scraper ranks lower
        # than title hits) still surface in the candidate list.
        def _run():
            return gp_search(query, lang=lang, country=country, n_hits=25)

        results = await asyncio.to_thread(_run)
        q_lower = (query or "").lower().strip()

        out = []
        for r in results or []:
            app_id = r.get("appId")
            if not app_id:
                # Promoted entries occasionally appear with no appId — skip.
                continue
            title = r.get("title") or app_id
            developer = r.get("developer") or ""
            # Mark whether the developer name matched, so the UI can hint at it.
            dev_match = q_lower and q_lower not in title.lower() and q_lower in developer.lower()
            subtitle = developer + (f" · {r.get('score'):.1f}★" if r.get("score") else "")
            if dev_match:
                # When the match came via developer name (not title), italicise
                # the developer prefix so it's still distinguishable.
                subtitle = f"{developer} (developer match)" + (f" · {r.get('score'):.1f}★" if r.get("score") else "")
            out.append({
                "id": app_id,
                "title": title,
                "subtitle": subtitle,
                "icon_url": r.get("icon"),
                "config": {
                    "app_id": app_id,
                    "country": country,
                    "lang": lang,
                },
            })
        return out[:15]

    async def collect(self) -> AsyncIterator[CollectedItem]:
        from google_play_scraper import Sort, reviews as gp_reviews

        app_id = self.config.get("app_id")
        if not app_id:
            raise RuntimeError(
                "Google Play app_id is missing from source config. "
                "Delete and re-add the source — the search result you picked had no appId."
            )
        country = self.config.get("country", "us")
        lang = self.config.get("lang", "en")
        max_count = int(self.config.get("max_count", 500))

        def _run():
            result, _ = gp_reviews(
                app_id, lang=lang, country=country, sort=Sort.NEWEST, count=max_count,
            )
            return result

        items = await asyncio.to_thread(_run)
        for item in items or []:
            posted = item.get("at")
            if isinstance(posted, str):
                try:
                    posted = datetime.fromisoformat(posted.replace("Z", "+00:00"))
                except Exception:
                    posted = None
            yield CollectedItem(
                external_id=str(item.get("reviewId")),
                text=item.get("content") or "",
                author=item.get("userName"),
                posted_at=posted,
                rating=float(item["score"]) if item.get("score") is not None else None,
                url=None,
                raw=json_safe({k: v for k, v in item.items() if k != "userImage"}),
            )
