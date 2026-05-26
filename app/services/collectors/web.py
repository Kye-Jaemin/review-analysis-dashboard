from __future__ import annotations

import asyncio
import hashlib
import time
import urllib.robotparser
from datetime import datetime
from typing import AsyncIterator, Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.services.collectors.base import CollectedItem, CollectorBase, json_safe

_last_request_by_domain: dict[str, float] = {}
_DOMAIN_SLEEP = 1.0


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower()


async def _polite_wait(url: str) -> None:
    dom = _domain(url)
    last = _last_request_by_domain.get(dom, 0.0)
    delta = time.monotonic() - last
    if delta < _DOMAIN_SLEEP:
        await asyncio.sleep(_DOMAIN_SLEEP - delta)
    _last_request_by_domain[dom] = time.monotonic()


def _robots_allowed(url: str, user_agent: str) -> bool:
    try:
        parsed = urlparse(url)
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
        rp.read()
        return rp.can_fetch(user_agent, url)
    except Exception:
        return True


def _extract_with_selectors(html: str, cfg: dict, url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    item_sel = (cfg.get("item_selector") or "").strip()
    items: list[dict] = []

    def _txt(node, sel):
        if not sel or not node:
            return None
        el = node.select_one(sel)
        return el.get_text(strip=True) if el else None

    if item_sel:
        for node in soup.select(item_sel):
            text = _txt(node, cfg.get("text_selector")) or node.get_text(" ", strip=True)
            if not text or len(text) < 5:
                continue
            items.append({
                "text": text,
                "author": _txt(node, cfg.get("author_selector")),
                "date_str": _txt(node, cfg.get("date_selector")),
                "rating_str": _txt(node, cfg.get("rating_selector")),
            })
    else:
        try:
            from readability import Document
            doc = Document(html)
            content_html = doc.summary()
            text = BeautifulSoup(content_html, "lxml").get_text("\n", strip=True)
        except Exception:
            text = soup.get_text("\n", strip=True)
        text = text.strip()
        if text:
            items.append({"text": text[:5000], "author": None, "date_str": None, "rating_str": None})
    return items


async def _fetch_static(url: str) -> Optional[str]:
    headers = {"User-Agent": settings.SCRAPER_USER_AGENT}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers) as client:
        resp = await client.get(url)
        if resp.status_code >= 400:
            return None
        return resp.text


async def _fetch_dynamic(url: str, wait_for: str = "", scroll: int = 0) -> Optional[str]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(user_agent=settings.SCRAPER_USER_AGENT)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if wait_for:
                try:
                    await page.wait_for_selector(wait_for, timeout=15000)
                except Exception:
                    pass
            for _ in range(int(scroll or 0)):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(800)
            html = await page.content()
        finally:
            await browser.close()
    return html


def _parse_rating(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    digits = "".join(ch for ch in s if ch.isdigit() or ch == ".")
    try:
        v = float(digits)
        if v > 5:
            v = v / 2 if v <= 10 else None
        return v
    except Exception:
        return None


class WebCollector(CollectorBase):
    async def collect(self) -> AsyncIterator[CollectedItem]:
        urls: list[str] = self.config.get("urls") or []
        dynamic = bool(self.config.get("dynamic"))
        wait_for = self.config.get("wait_for") or ""
        scroll = int(self.config.get("scroll") or 0)

        for url in urls:
            if not url.startswith(("http://", "https://")):
                continue
            if not _robots_allowed(url, settings.SCRAPER_USER_AGENT):
                continue
            await _polite_wait(url)

            html = None
            if dynamic and settings.PLAYWRIGHT_ENABLED:
                html = await _fetch_dynamic(url, wait_for=wait_for, scroll=scroll)
            if not html:
                html = await _fetch_static(url)
            if not html:
                continue

            items = _extract_with_selectors(html, self.config, url)
            for idx, item in enumerate(items):
                text = item.get("text") or ""
                ext = hashlib.sha1(f"{url}|{idx}|{text[:200]}".encode("utf-8")).hexdigest()
                yield CollectedItem(
                    external_id=ext,
                    text=text,
                    author=item.get("author"),
                    posted_at=None,
                    rating=_parse_rating(item.get("rating_str")),
                    url=url,
                    raw=json_safe({"source_url": url, "index": idx, "raw_date": item.get("date_str")}),
                )
