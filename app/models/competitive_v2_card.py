from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CompetitiveV2Card(Base):
    """Saved bottom-up success-factor clustering for a CSV.

    Companion table to competitive_factor_cards (v1) but with no
    factors/criteria input — the clustering is data-driven. Stores
    the parsed CSV row set in `input_csv` and the LLM's clustering
    output verbatim in `result_payload` so loads cost zero Claude
    calls. Re-analyze re-runs the LLM on the saved CSV.
    """

    __tablename__ = "competitive_v2_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    model_used: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # The /vendors CSV row set the analysis was run on.
    input_csv: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    # Full analyze_csv_v2() return dict — see the service for the shape.
    result_payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
