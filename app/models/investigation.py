from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Investigation(Base):
    """A named (source_ids, root_ids) filter combination that the user can
    click on the dashboard to scope every panel to that subset."""

    __tablename__ = "investigations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    root_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # User-controlled card order on the dashboard. Lower = earlier. The
    # /api/investigations endpoint sorts by this primarily, falling back
    # to updated_at desc when display_order matches. New rows get the
    # next available value so they land at the end of the grid; the user
    # can then drag them anywhere.
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
