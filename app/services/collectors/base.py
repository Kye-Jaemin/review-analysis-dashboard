from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, AsyncIterator, Optional

from app.models.source import Source


def json_safe(value: Any) -> Any:
    """Recursively convert non-JSON-serializable values (datetime/date/set/bytes)
    so they can be stored in a JSON column via asyncpg."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return None
    if isinstance(value, (set, frozenset, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    # Fallback: stringify unknown types (e.g. custom scraper objects)
    return str(value)


@dataclass
class CollectedItem:
    external_id: str
    text: str
    author: Optional[str] = None
    posted_at: Optional[datetime] = None
    rating: Optional[float] = None
    url: Optional[str] = None
    raw: dict = field(default_factory=dict)


class CollectorBase:
    def __init__(self, source: Source):
        self.source = source
        self.config = source.config or {}

    async def collect(self) -> AsyncIterator[CollectedItem]:
        raise NotImplementedError
        yield  # pragma: no cover

    @classmethod
    async def search(cls, query: str, **kwargs) -> list[dict]:
        """Return candidate list for the 'name search' flow. Each candidate dict must include:
        - id (str, internal display id)
        - title (str)
        - subtitle (str, e.g. developer or subscribers)
        - icon_url (str | None)
        - config (dict, partial Source.config to merge on save)
        """
        return []
