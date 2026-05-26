from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator, Optional

from app.models.source import Source


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
