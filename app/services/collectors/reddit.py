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
                        "time_filter": "year",
                        "include_comments": True,
                        "max_submissions": 500,
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
        time_filter = self.config.get("time_filter", "year")
        search_query = (self.config.get("search_query") or "").strip()
        max_submissions = int(self.config.get("max_submissions") or self.config.get("max_count") or 500)
        include_comments = bool(self.config.get("include_comments", True))
        max_comments = int(self.config.get("max_comments_per_submission", 20))

        # The previous version built ALL items into a list in a single
        # thread call, then yielded them after the fact — so the
        # progress bar sat at 0% for the entire 4-10 minute walk and
        # then jumped to 100% all at once. Stream items via an
        # asyncio.Queue instead: a worker thread runs PRAW synchronously
        # and pushes items as they're scraped; the async generator below
        # awaits the queue and yields each item as it arrives. The
        # collection route's `fetched += 1` then updates the progress
        # bar in real time.
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=128)
        _DONE = object()  # sentinel marking end-of-stream

        def _worker():
            try:
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

                def _put(item: dict):
                    # call_soon_threadsafe + put_nowait would race against
                    # the queue's maxsize; using run_coroutine_threadsafe
                    # blocks the worker until there's room, providing
                    # natural backpressure when the consumer is slow.
                    fut = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
                    fut.result()

                for submission in listing:
                    body = (submission.title or "")
                    if submission.selftext:
                        body = body + "\n\n" + submission.selftext
                    _put({
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
                            _put({
                                "kind": "comment",
                                "external_id": f"t1_{c.id}",
                                "text": c.body,
                                "author": str(c.author) if c.author else None,
                                "posted_at": datetime.utcfromtimestamp(c.created_utc),
                                "url": f"https://reddit.com{submission.permalink}{c.id}/",
                                "raw": {"score": c.score, "parent_submission": submission.id},
                            })
                    time.sleep(0.5)
            except Exception as e:
                # Surface the failure to the async side rather than
                # silently exiting and leaving the consumer waiting.
                asyncio.run_coroutine_threadsafe(queue.put(e), loop).result()
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(_DONE), loop).result()

        # Start the PRAW worker on a thread and stream items off the
        # queue. asyncio.to_thread spawns a thread from the default
        # executor — we don't await it here; the worker drives the
        # queue and signals end-of-stream itself.
        worker_task = asyncio.create_task(asyncio.to_thread(_worker))
        try:
            while True:
                item = await queue.get()
                if item is _DONE:
                    break
                if isinstance(item, Exception):
                    raise item
                yield CollectedItem(
                    external_id=item["external_id"],
                    text=item["text"],
                    author=item["author"],
                    posted_at=item["posted_at"],
                    rating=None,
                    url=item["url"],
                    raw=json_safe(item["raw"]),
                )
        finally:
            # Make sure the worker task is awaited even if the consumer
            # bailed early (e.g. max_count hit upstream) so any pending
            # exception propagates and the thread is joined.
            try:
                await worker_task
            except Exception:
                pass
