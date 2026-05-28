from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AutoCategory(Base):
    """LLM-derived category for a specific Investigation card. Re-running auto
    analysis replaces the whole set for that card.

    `language` records the language in which `name` / `description` were
    originally generated (the analysis-time summary_lang). `translations`
    caches on-demand translations keyed by target language code, e.g.
    `{"ko": {"name": "...", "description": "..."}}`. The display layer
    asks for the user's UI language and falls back to the original fields
    when the cache is missing the entry."""

    __tablename__ = "auto_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investigation_id: Mapped[int] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    review_count: Mapped[int] = mapped_column(Integer, default=0)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    language: Mapped[str] = mapped_column(String(10), nullable=False, default="en")
    translations: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
