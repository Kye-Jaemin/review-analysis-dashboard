from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import AsyncIterator

from app.config import settings
from app.services.collectors.base import CollectedItem, CollectorBase, json_safe


def _make_reddit():
    import praw

    if not (settings.REDDIT_CLIENT_ID and settings.REDDIT_CLIENT_SECRET):
        raise RuntimeError(
            "Reddit credentials missing. Set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET in .env "
            "(create a 'script' app at https://www.reddit.com/prefs/apps)."
        )
    reddit = praw.Reddit(
        client_id=settings.REDDIT_CLIENT_ID,
        client_secret=settings.REDDIT_CLIENT_SECRET,
        user_agent=settings.REDDIT_USER_AGENT,
    )
    reddit.read_only = True
    return reddit


class RedditCollector(CollectorBase):
    @classmethod
    async def search(cls, query: str, **kwargs) -> list[dict]:
        def _run():
            reddit = _make_reddit()
            results = list(reddit.subreddits.search(query, limit=10))
            out = []
            for sr in results:
                icon = getattr(sr, "community_icon", None) or getattr(sr, "icon_img", None) or None
                if icon and "?" in icon:
                    icon = icon.split("?")[0]
                out.append({
                    "id": sr.display_name,
                    "title": f"r/{sr.display_name}",
                    "subtitle": f"{getattr(sr, 'subscribers', 0):,} subscribers · {(getattr(sr, 'public_description', '') or '')[:80]}",
                    "icon_url": icon,
                    "config": {
                        "subreddit": sr.display_name,
                        "sort": "new",
                        "time_filter": "month",
                        "include_comments": True,
                        "max_submissions": 50,
                        "max_comments_per_submission": 20,
                    },
                })
            return out

        try:
            return await asyncio.to_thread(_run)
        except Exception as e:
            return [{"id": "_error", "title": "Reddit search failed", "subtitle": str(e), "icon_url": None, "config": {}}]

    async def collect(self) -> AsyncIterator[CollectedItem]:
        subreddit_name = self.config["subreddit"]
        sort = self.config.get("sort", "new")
        time_filter = self.config.get("time_filter", "month")
        search_query = (self.config.get("search_query") or "").strip()
        max_submissions = int(self.config.get("max_submissions") or self.config.get("max_count") or 50)
        include_comments = bool(self.config.get("include_comments", True))
        max_comments = int(self.config.get("max_comments_per_submission", 20))

        def _gather():
            reddit = _make_reddit()
            sub = reddit.subreddit(subreddit_name)
            if search_query:
                listing = sub.search(search_query, sort=sort, time_filter=time_filter, limit=max_submissions)
            elif sort == "top":
                listing = sub.top(time_filter=time_filter, limit=max_submissions)
            elif sort == "hot":
                listing = sub.hot(limit=max_submissions)
            else:
                listing = sub.new(limit=max_submissions)

            collected = []
            for submission in listing:
                body = (submission.title or "")
                if submission.selftext:
                    body = body + "\n\n" + submission.selftext
                collected.append({
                    "kind": "submission",
                    "external_id": f"t3_{submission.id}",
                    "text": body.strip(),
                    "author": str(submission.author) if submission.author else None,
                    "posted_at": datetime.utcfromtimestamp(submission.created_utc),
                    "url": f"https://reddit.com{submission.permalink}",
                    "raw": {
                        "score": submission.score,
                        "num_comments": submission.num_comments,
                        "title": submission.title,
                    },
                })

                if include_comments and max_comments > 0:
                    try:
                        submission.comments.replace_more(limit=0)
                        comments = submission.comments.list()[:max_comments]
                    except Exception:
                        comments = []
                    for c in comments:
                        if not getattr(c, "body", None):
                            continue
                        collected.append({
                            "kind": "comment",
                            "external_id": f"t1_{c.id}",
                            "text": c.body,
                            "author": str(c.author) if c.author else None,
                            "posted_at": datetime.utcfromtimestamp(c.created_utc),
                            "url": f"https://reddit.com{submission.permalink}{c.id}/",
                            "raw": {"score": c.score, "parent_submission": submission.id},
                        })
                time.sleep(0.5)
            return collected

        items = await asyncio.to_thread(_gather)
        for item in items:
            yield CollectedItem(
                external_id=item["external_id"],
                text=item["text"],
                author=item["author"],
                posted_at=item["posted_at"],
                rating=None,
                url=item["url"],
                raw=json_safe(item["raw"]),
            )
