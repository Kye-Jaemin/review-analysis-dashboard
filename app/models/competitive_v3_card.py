from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CompetitiveV3Card(Base):
    """Saved LLM categorization for an uploaded /vendors export.

    Mirrors the competitive_v2_cards pattern but persists a richer
    payload: both the original per-reason rows (enriched with the
    LLM's `카테고리` assignment) and the build_categorized_view
    output so re-loading a saved card costs zero Claude calls.

    Re-exporting the categorized XLSX is also driven from the
    persisted `input_rows` — see export_categorized_xlsx().
    """

    __tablename__ = "competitive_v3_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    model_used: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # Original uploaded filename — surfaced in the saved-card list so
    # the user can tell two analyses apart at a glance.
    input_filename: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Raw per-reason rows AFTER the 카테고리 column was added (either by
    # the LLM or — if the upload already had it — passed through).
    input_rows: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    # Full build_categorized_view() output. Lets loads bypass re-grouping.
    result_payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    # Human-curated grouping of the AI categories into higher-level
    # "competitive criteria". Shape:
    #   {"groups": [{"name": str, "categories": [str, ...]}, ...]}
    # Stored in its own column (not folded into result_payload) so it
    # survives the self-heal rebuild of result_payload on card load.
    # NULL until the user runs / saves a criteria mapping.
    criteria_mapping: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )
    hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
