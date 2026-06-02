from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CompetitiveFactorCard(Base):
    """A saved snapshot of a /competitive analysis.

    Each row stores the full `rank_vendors_by_factor()` JSON result for a
    specific (factor, threshold, model) tuple, so the dashboard can show
    that ranking again without re-paying for the Claude completion.

    Snapshots are point-in-time on purpose — no unique constraint on
    `factor`. The same factor analyzed today vs next week creates two
    separate cards so the user can compare drift. Cleanup is manual via
    the delete button (hidden flag for soft delete, like Investigation).

    `universe_size` records the count of distinct auto-categories that
    existed at scoring time. Storing it alongside the result lets the UI
    flag drift — "saved when 90 categories existed, now 95" — so the
    user knows when a re-analyze might surface new matches.
    """

    __tablename__ = "competitive_factor_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Original user input. Always kept as the source-of-truth string we'd
    # send back to the LLM on re-analyze.
    factor: Mapped[str] = mapped_column(String(200), nullable=False)
    # Display name shown in the sidebar. Defaults to `factor` at save
    # time; user can rename via PATCH for clarity ("AI 코칭 v2", "경쟁사
    # 비교용", etc.) without losing the original LLM input.
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    # Snapshot of the parameters used for this analysis. Kept for
    # reproducibility + so the "re-analyze" button can reuse the same
    # threshold the user originally picked.
    threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    model_used: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # Distinct auto-category count at scoring time. Powers the drift
    # indicator in the saved-card header.
    universe_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The full rank_vendors_by_factor() output. JSON gets opaque to the
    # ORM but we never query into it — every read is "give me this
    # row's result verbatim".
    result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    # Same hide/unhide pattern as Investigation — soft delete first,
    # hard delete via explicit DELETE.
    hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # User-controlled order in the sidebar. Lower = earlier; ties broken
    # by updated_at desc so freshly re-analyzed cards float to the top
    # within the same drag position.
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
