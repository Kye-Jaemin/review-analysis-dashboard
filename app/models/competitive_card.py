from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CompetitiveFactorCard(Base):
    """A saved /competitive (경쟁력 분류) analysis.

    NEW FLOW (post-migration 0017):
      1. User uploads a CSV exported from /vendors (strengths-only or both).
      2. User types a competitive factor (e.g. "음식 Vision AI 정확도").
      3. Claude scores each distinct category in the CSV against the
         factor; rows that pass the threshold get a `relevance` field.
      4. The full input CSV is snapshotted in `input_csv` so the card
         stays reproducible even if the underlying DB drifts; the
         relevance-filtered rows live in `result_rows`.

    The old columns (universe_size, result) survive as nullable so the
    table doesn't need to be rebuilt; the new code writes default 0/{}
    there. They aren't read anymore.

    Snapshots are point-in-time — no unique constraint on factor.
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
    # ---- New CSV-driven fields (migration 0017) ----
    # Full row set the user uploaded — each entry mirrors a row of the
    # /vendors export CSV: {vendor, type, category, pct, count,
    # wilson_score, description, small_sample, reasons}. Stored as JSON
    # so re-analyze stays a pure DB read on the input side.
    input_csv: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(
        JSON, nullable=True
    )
    # Rows whose `relevance` cleared the threshold, with the score
    # attached. Each entry = an input row + {"relevance": float,
    # "match_score": float}.
    result_rows: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(
        JSON, nullable=True
    )
    # ---- Legacy fields, nullable. Not read by new code. ----
    universe_size: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, default=0
    )
    result: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON, nullable=True, default=dict
    )
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
