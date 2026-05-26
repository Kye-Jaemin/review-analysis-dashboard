from __future__ import annotations

import asyncio
from datetime import datetime
from typing import AsyncIterator

from app.services.collectors.base import CollectedItem, CollectorBase


class GooglePlayCollector(CollectorBase):
    @classmethod
    async def search(cls, query: str, country: str = "us", lang: str = "en", **kwargs) -> list[dict]:
        from google_play_scraper import search as gp_search

        def _run():
            return gp_search(query, lang=lang, country=country, n_hits=10)

        results = await asyncio.to_thread(_run)
        out = []
        for r in results or []:
            out.append({
                "id": r.get("appId"),
                "title": r.get("title") or r.get("appId"),
                "subtitle": (r.get("developer") or "") + (f" · {r.get('score'):.1f}★" if r.get("score") else ""),
                "icon_url": r.get("icon"),
                "config": {
                    "app_id": r.get("appId"),
                    "country": country,
                    "lang": lang,
                },
            })
        return out

    async def collect(self) -> AsyncIterator[CollectedItem]:
        from google_play_scraper import Sort, reviews as gp_reviews

        app_id = self.config["app_id"]
        country = self.config.get("country", "us")
        lang = self.config.get("lang", "en")
        max_count = int(self.config.get("max_count", 100))

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
                raw={k: v for k, v in item.items() if k != "userImage"},
            )
