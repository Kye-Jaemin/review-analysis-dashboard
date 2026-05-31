from __future__ import annotations

import hashlib
from datetime import datetime
from typing import AsyncIterator

import httpx

from app.services.collectors.base import CollectedItem, CollectorBase, json_safe

# Apple's RSS endpoint returns up to 50 reviews per page, pages 1–10.
_RSS_URL = "https://itunes.apple.com/{country}/rss/customerreviews/id={app_id}/sortBy=mostRecent/page={page}/json"
_MAX_PAGE = 10


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Apple returns e.g. "2026-05-25T10:23:45-07:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _label(node, key: str = "label") -> str:
    if isinstance(node, dict):
        v = node.get(key)
        return v if isinstance(v, str) else ""
    return ""


class AppStoreCollector(CollectorBase):
    @classmethod
    async def search(cls, query: str, country: str = "us", **kwargs) -> list[dict]:
        # Two passes:
        #   1. default search — iTunes matches across name/developer/description
        #      but title hits dominate the top of the list.
        #   2. explicit `attribute=softwareDeveloper` — forces a developer-name
        #      match. Merged + dedup'd by trackId so apps from the searched
        #      company show up even when the query isn't part of the app name.
        merged: dict = {}
        async with httpx.AsyncClient(timeout=10) as client:
            resp_default = await client.get(
                "https://itunes.apple.com/search",
                params={"term": query, "country": country, "media": "software", "limit": 12},
            )
            for r in (resp_default.json().get("results") or []):
                tid = r.get("trackId")
                if tid and tid not in merged:
                    merged[tid] = r

            resp_dev = await client.get(
                "https://itunes.apple.com/search",
                params={
                    "term": query, "country": country, "media": "software",
                    "limit": 8, "attribute": "softwareDeveloper",
                },
            )
            for r in (resp_dev.json().get("results") or []):
                tid = r.get("trackId")
                if tid and tid not in merged:
                    r["_via_developer"] = True
                    merged[tid] = r

        out = []
        for r in merged.values():
            track_id = r.get("trackId")
            if not track_id:
                continue
            rating = r.get("averageUserRating")
            # Apple's official URL slug lives in trackViewUrl, e.g.
            #   https://apps.apple.com/us/app/cal-ai-calorie-tracker/id6480417616?uo=4
            slug = ""
            track_url = r.get("trackViewUrl") or ""
            if "/app/" in track_url:
                tail = track_url.split("/app/", 1)[1]
                if "/id" in tail:
                    slug = tail.split("/id", 1)[0]
            if not slug:
                slug = (r.get("trackName") or "").strip().lower().replace(" ", "-")
            subtitle = (r.get("artistName") or "") + (f" · {rating:.1f}★" if rating else "")
            if r.get("_via_developer"):
                subtitle = f"{r.get('artistName') or ''} (developer match)" + (f" · {rating:.1f}★" if rating else "")
            out.append({
                "id": str(track_id),
                "title": r.get("trackName") or "",
                "subtitle": subtitle,
                "icon_url": r.get("artworkUrl100"),
                "config": {
                    "app_id": int(track_id),
                    "app_name": slug,
                    "country": country,
                },
            })
        return out

    async def collect(self) -> AsyncIterator[CollectedItem]:
        country = (self.config.get("country") or "us").lower()
        app_id = self.config.get("app_id")
        if not app_id:
            raise RuntimeError(
                "App Store source config is missing app_id. "
                "Delete and re-add the source."
            )
        max_count = int(self.config.get("max_count", 500))

        emitted = 0
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": "review-collector/0.1"},
        ) as client:
            for page in range(1, _MAX_PAGE + 1):
                if emitted >= max_count:
                    break
                url = _RSS_URL.format(country=country, app_id=app_id, page=page)
                resp = await client.get(url)
                if resp.status_code != 200:
                    # Apple sometimes returns 403 / 503 mid-pagination; treat as end.
                    break
                try:
                    data = resp.json()
                except Exception:
                    break

                entries = (data.get("feed") or {}).get("entry") or []
                # Apple's RSS wraps a single entry in a dict instead of a list when only one exists.
                if isinstance(entries, dict):
                    entries = [entries]
                if not entries:
                    # On the FIRST page, an empty feed almost always means
                    # Apple's public RSS isn't serving reviews for this
                    # (app_id, country) pair — a known quirk of the
                    # /customerreviews endpoint that hits a number of apps,
                    # esp. in the US store (e.g. MacroFactor 1553503471).
                    # Raise an explicit error so the user sees "try a
                    # different country" instead of "0 new" with no clue.
                    if page == 1 and emitted == 0:
                        raise RuntimeError(
                            f"Apple's public review feed for app_id={app_id} "
                            f"in country '{country}' is empty. This is a known "
                            f"Apple RSS limitation for certain apps/stores — "
                            f"the reviews are still on the App Store website but "
                            f"the public RSS endpoint doesn't serve them. Try "
                            f"adding the source with country=gb (UK) or jp (Japan) "
                            f"instead, which often work when US/CA/AU don't."
                        )
                    break

                # The first entry on page 1 is the app metadata, not a review.
                # Reviews always carry an "author" + "content" + "im:rating".
                page_reviews = [e for e in entries if "im:rating" in e and "content" in e]
                if not page_reviews:
                    if page == 1 and emitted == 0:
                        raise RuntimeError(
                            f"Apple's RSS feed for app_id={app_id} in country "
                            f"'{country}' returned only app metadata, no reviews. "
                            f"This usually means the public RSS isn't serving "
                            f"reviews for this store. Try country=gb or jp."
                        )
                    break

                for e in page_reviews:
                    if emitted >= max_count:
                        break
                    ext_id_raw = _label(e.get("id"))
                    if not ext_id_raw:
                        # Build a stable fallback hash if Apple didn't send an id.
                        ext_id_raw = hashlib.sha1(
                            (_label(e.get("title")) + "|" +
                             _label(e.get("content")) + "|" +
                             _label((e.get("author") or {}).get("name"))).encode("utf-8")
                        ).hexdigest()

                    title = _label(e.get("title"))
                    content = _label(e.get("content"))
                    text = (title + "\n\n" + content).strip() if title else content
                    author = _label((e.get("author") or {}).get("name"))
                    rating_str = _label(e.get("im:rating"))
                    try:
                        rating = float(rating_str) if rating_str else None
                    except ValueError:
                        rating = None
                    posted_at = _parse_dt(_label(e.get("updated")))

                    yield CollectedItem(
                        external_id=str(ext_id_raw),
                        text=text,
                        author=author or None,
                        posted_at=posted_at,
                        rating=rating,
                        url=_label((e.get("author") or {}).get("uri")) or None,
                        raw=json_safe(e),
                    )
                    emitted += 1
