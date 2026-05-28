from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ThemeSnapshot(Base):
    """Persisted mind-map result. Each snapshot belongs to an Investigation
    card; deleting the card cascades to its snapshots."""

    __tablename__ = "theme_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investigation_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    sentiment: Mapped[str] = mapped_column(String(50), nullable=False)
    source_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    root_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    auto_category_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    summary_lang: Mapped[str] = mapped_column(String(10), default="en")
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    themes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
