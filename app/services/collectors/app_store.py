from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from typing import AsyncIterator

import httpx

from app.services.collectors.base import CollectedItem, CollectorBase


class AppStoreCollector(CollectorBase):
    @classmethod
    async def search(cls, query: str, country: str = "us", **kwargs) -> list[dict]:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://itunes.apple.com/search",
                params={"term": query, "country": country, "media": "software", "limit": 10},
            )
            data = resp.json()
        out = []
        for r in data.get("results", []) or []:
            track_id = r.get("trackId")
            if not track_id:
                continue
            rating = r.get("averageUserRating")
            # Apple's official URL slug lives in trackViewUrl, e.g.
            #   https://apps.apple.com/us/app/cal-ai-calorie-tracker/id6480417616?uo=4
            # Extract the slug between '/app/' and '/id'. Deriving it from the
            # trackName is unreliable because Apple's slug rules don't always
            # match a naive lowercase-+-hyphenate of the display name.
            slug = ""
            track_url = r.get("trackViewUrl") or ""
            if "/app/" in track_url:
                tail = track_url.split("/app/", 1)[1]
                if "/id" in tail:
                    slug = tail.split("/id", 1)[0]
            if not slug:
                slug = (r.get("trackName") or "").strip().lower().replace(" ", "-")
            out.append({
                "id": str(track_id),
                "title": r.get("trackName") or "",
                "subtitle": (r.get("artistName") or "") + (f" · {rating:.1f}★" if rating else ""),
                "icon_url": r.get("artworkUrl100"),
                "config": {
                    "app_id": int(track_id),
                    "app_name": slug,
                    "country": country,
                },
            })
        return out

    async def collect(self) -> AsyncIterator[CollectedItem]:
        from app_store_scraper import AppStore

        country = self.config.get("country", "us")
        app_name = self.config.get("app_name") or ""
        app_id = self.config.get("app_id")
        if not app_id or not app_name:
            raise RuntimeError(
                "App Store source config is missing app_id or app_name (URL slug). "
                "Delete and re-add the source so the slug is rebuilt from trackViewUrl."
            )
        max_count = int(self.config.get("max_count", 100))

        def _run():
            app = AppStore(country=country, app_name=app_name, app_id=app_id)
            app.review(how_many=max_count)
            return app.reviews or []

        items = await asyncio.to_thread(_run)
        for item in items:
            user = str(item.get("userName") or "")
            date = item.get("date")
            title = str(item.get("title") or "")
            ext = hashlib.sha1(f"{user}|{date}|{title}".encode("utf-8")).hexdigest()
            posted = None
            if isinstance(date, datetime):
                posted = date
            elif isinstance(date, str):
                try:
                    posted = datetime.fromisoformat(date.replace("Z", "+00:00"))
                except Exception:
                    pass
            yield CollectedItem(
                external_id=ext,
                text=(item.get("review") or "").strip(),
                author=user or None,
                posted_at=posted,
                rating=float(item["rating"]) if item.get("rating") is not None else None,
                url=None,
                raw={k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in item.items()},
            )
