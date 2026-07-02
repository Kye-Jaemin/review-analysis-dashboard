from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class VendorCategory(Base):
    """A user-named group of existing Investigation cards (e.g. "헬스"),
    used to scope /vendors (and, indirectly via its Excel export, the
    /competitive-v3 upload flow) to a subset of vendors.

    Mirrors Investigation's field pattern (label/description/display_order/
    hidden/timestamps). Membership is a soft reference — `investigation_ids`
    is a plain JSON list of Investigation.id, not a foreign key — so a card
    disappearing doesn't break the category; readers self-heal by dropping
    dead ids. This is the same derived-reference philosophy Investigation
    itself uses for source_ids/root_ids against Source/Category.
    """

    __tablename__ = "vendor_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    investigation_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Same "drag anywhere" ordering convention as Investigation.display_order.
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Same display-only visibility flag as Investigation.hidden.
    hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
