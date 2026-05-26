"""Simple dict-based i18n. Cookie 'lang' stores user preference (1 year)."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

from fastapi import Request

SUPPORTED = ("en", "ko")
COOKIE_NAME = "lang"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year

_I18N_DIR = Path(__file__).parent


@lru_cache
def _load(lang: str) -> dict:
    path = _I18N_DIR / f"{lang}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def detect_lang(request: Request) -> str:
    from app.config import settings

    cookie = request.cookies.get(COOKIE_NAME)
    if cookie in SUPPORTED:
        return cookie
    accept = request.headers.get("accept-language", "")
    for part in accept.split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in SUPPORTED:
            return code
    return settings.DEFAULT_LANGUAGE if settings.DEFAULT_LANGUAGE in SUPPORTED else "en"


def translate(lang: str, key: str, **kwargs) -> str:
    data = _load(lang)
    value: object = data
    for part in key.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            # fallback to English
            value = _load("en")
            for p2 in key.split("."):
                if isinstance(value, dict) and p2 in value:
                    value = value[p2]
                else:
                    return key
            break
    text = value if isinstance(value, str) else key
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError):
            pass
    return text


def make_t(lang: str):
    def t(key: str, **kwargs) -> str:
        return translate(lang, key, **kwargs)

    return t
