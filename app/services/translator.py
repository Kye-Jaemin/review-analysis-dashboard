"""Translation helpers.

Two flavours, both routed through Claude so we don't add a second LLM
dependency for translation only:

  1. `translate_auto_categories(session, cats, target_lang)`
     Batch-translates a list of AutoCategory rows whose stored language
     differs from the requested UI language. Result is cached in each
     row's `translations` JSON column keyed by target lang code, so the
     same UI language hit is free on subsequent requests. Returns the
     list of {id, name, description} dicts ready for the API response.

  2. `translate_text(text, target_lang)`
     Single-shot translation for review text. Reviews are user-generated
     and ephemeral in the UI — we don't persist these; the dashboard
     calls this on demand when the user clicks "translate" on a row.

The translation cache is only ever written, never invalidated, because
auto-category content is itself immutable (a re-analysis deletes and
re-creates the whole set, so a fresh `translations={}` starts over).
"""
from __future__ import annotations

import json
import re
from typing import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AutoCategory


# Same labels we use for analysis prompts so the LLM gets a consistent name.
_LANG_LABELS = {
    "en": "English",
    "ko": "Korean",
}


def _lang_label(code: str) -> str:
    return _LANG_LABELS.get((code or "").lower(), "English")


def _norm_lang(code: str | None) -> str:
    return (code or "en").strip().lower() or "en"


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    return s.strip()


def _pluck_cached(cat: AutoCategory, target_lang: str) -> dict | None:
    """Return cached translation for `target_lang` if present, else None.
    Treats the row's own `language` as a no-op (returns the original fields)."""
    target = _norm_lang(target_lang)
    if _norm_lang(cat.language) == target:
        return {"name": cat.name, "description": cat.description}
    cache = cat.translations or {}
    hit = cache.get(target)
    if isinstance(hit, dict) and hit.get("name"):
        return {
            "name": hit.get("name") or cat.name,
            "description": hit.get("description") if hit.get("description") is not None else cat.description,
        }
    return None


async def translate_auto_categories(
    session: AsyncSession,
    cats: Iterable[AutoCategory],
    target_lang: str,
    *,
    model: str | None = None,
) -> dict[int, dict]:
    """Return {auto_cat_id: {"name": ..., "description": ...}} for every input
    category, translated to `target_lang`. Cache hits and same-language rows
    cost nothing; only missing entries hit the LLM. New translations are
    persisted to the `translations` JSON column on the same session.

    Falls back to the original name/description silently when the LLM is
    unavailable or returns malformed output — the UI should never break
    just because translation failed."""
    target = _norm_lang(target_lang)
    cats = list(cats)
    out: dict[int, dict] = {}
    pending: list[AutoCategory] = []

    for c in cats:
        cached = _pluck_cached(c, target)
        if cached is not None:
            out[c.id] = cached
        else:
            pending.append(c)

    if not pending:
        return out

    # Fall back to originals if we can't call the LLM.
    if not settings.ANTHROPIC_API_KEY:
        for c in pending:
            out[c.id] = {"name": c.name, "description": c.description}
        return out

    from anthropic import AsyncAnthropic

    target_label = _lang_label(target)
    items = [
        {
            "id": c.id,
            "source_language": _lang_label(c.language) or "the original language",
            "name": c.name,
            "description": c.description or "",
        }
        for c in pending
    ]
    system = (
        f"You translate short category labels and one-line rubrics into {target_label}.\n"
        f"Keep meaning, tone, and length close to the original. Don't add explanations.\n"
        f"Respond with ONLY a JSON array of objects, no prose, no markdown fences:\n"
        f'  [{{"id": <int>, "name": "<translated name>", '
        f'"description": "<translated description, empty string if input was empty>"}}, ...]\n'
        f"Preserve every input id exactly."
    )
    user = "Translate these into " + target_label + ":\n" + json.dumps(items, ensure_ascii=False)

    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    try:
        resp = await client.messages.create(
            model=model or settings.ANTHROPIC_MODEL,
            max_tokens=2048,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = _strip_fences("".join(getattr(b, "text", "") for b in resp.content))
        data = json.loads(text)
    except Exception:
        data = []

    by_id: dict[int, dict] = {}
    if isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            try:
                rid = int(entry.get("id"))
            except (TypeError, ValueError):
                continue
            name = (entry.get("name") or "").strip()
            desc = entry.get("description")
            if desc is not None:
                desc = (desc or "").strip()
            if not name:
                continue
            by_id[rid] = {"name": name[:200], "description": desc or None}

    # Persist whatever we got and fall back to originals for anything missing.
    for c in pending:
        t = by_id.get(c.id)
        if t is None:
            out[c.id] = {"name": c.name, "description": c.description}
            continue
        # Write back to the cache. SQLAlchemy doesn't see in-place dict
        # mutation by default, so reassign.
        cache = dict(c.translations or {})
        cache[target] = {"name": t["name"], "description": t["description"]}
        c.translations = cache
        out[c.id] = t

    try:
        await session.commit()
    except Exception:
        await session.rollback()

    return out


async def translate_text(
    text: str,
    target_lang: str,
    *,
    model: str | None = None,
) -> str:
    """Translate a single string (a review) into `target_lang`. Returns the
    original text on any failure so the UI degrades gracefully."""
    if not text or not text.strip():
        return text
    target = _norm_lang(target_lang)
    target_label = _lang_label(target)
    if not settings.ANTHROPIC_API_KEY:
        return text

    from anthropic import AsyncAnthropic

    system = (
        f"You translate user reviews into {target_label}.\n"
        f"Preserve meaning, tone, and any product names. Don't add commentary,\n"
        f"don't add quotes around the result, don't say 'Here is the translation'.\n"
        f"Respond with ONLY the translated text."
    )
    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    try:
        resp = await client.messages.create(
            model=model or settings.ANTHROPIC_MODEL,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": text[:4000]}],
        )
        out = "".join(getattr(b, "text", "") for b in resp.content).strip()
        return out or text
    except Exception:
        return text
