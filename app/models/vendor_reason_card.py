from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class VendorReasonCard(Base):
    """A saved "why?" analysis for a single (vendor, category, band) tuple.

    When the user clicks a strength or weakness on /vendors, the dashboard
    samples up to 100 reviews from that category × that vendor × that
    sentiment band (positive or negative) and asks Claude to identify the
    underlying REASONS — e.g. "WW 체중 감량 성과 공유" + positive →
    {지속 가능한 식단·습관 형성, 커뮤니티 응원, 포인트 시스템, …}.

    Storing each analysis as a row here means the modal can re-open
    instantly without burning another Claude call, and the user can
    rename / hide / explicitly re-analyze them.

    No unique constraint on (vendor_key, category_name, band) — by
    design, the user can save multiple snapshots of the same factor
    over time and compare drift, mirroring the CompetitiveFactorCard
    pattern.
    """

    __tablename__ = "vendor_reason_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Canonical vendor stem (e.g. "weightwatchers"). Used as the lookup
    # key in vendors.py's grouping logic.
    vendor_key: Mapped[str] = mapped_column(String(100), nullable=False)
    # Snapshot of the vendor's display name at save time so the card
    # title stays stable even if vendors.py's display picker changes.
    vendor_display: Mapped[str] = mapped_column(String(200), nullable=False)
    # The auto-category name that was clicked. Matched case-insensitively
    # in the vendors aggregation (same dedup-by-name logic).
    category_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # "positive" or "negative" — which sentiment direction this card
    # explains. Strength click → positive; weakness click → negative.
    band: Mapped[str] = mapped_column(String(20), nullable=False)
    # User-facing label. Defaults to category_name; user can PATCH to
    # something like "WW 체중 감량 — 1Q 분석".
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    model_used: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # How many unique reviews fed the LLM at analysis time. Lets the
    # modal show "표본 N개" and lets the user see when the corpus has
    # grown enough that re-analysis might surface new reasons.
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Vendor's source_ids at the moment of analysis. Stored as JSON so
    # drift detection can compare against the current source set.
    source_ids_snapshot: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    # The actual analysis output. Each entry:
    #   {"reason": str, "count": int, "examples": [str, str, str]}
    # `count` is the truthful sample-side count (not LLM's estimate) —
    # see vendor_reasons.py for the override step that mirrors the
    # themes.py fix.
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
