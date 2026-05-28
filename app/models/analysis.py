from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, Enum as SAEnum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

if TYPE_CHECKING:
    from app.models.review import Review
    from app.models.category import Category


class Sentiment(str, enum.Enum):
    very_positive = "very_positive"
    positive = "positive"
    neutral = "neutral"
    negative = "negative"
    very_negative = "very_negative"


SENTIMENT_TO_SCORE = {
    Sentiment.very_negative: 1,
    Sentiment.negative: 2,
    Sentiment.neutral: 3,
    Sentiment.positive: 4,
    Sentiment.very_positive: 5,
}

SCORE_TO_SENTIMENT = {v: k for k, v in SENTIMENT_TO_SCORE.items()}


class AnalysisStatus(str, enum.Enum):
    succeeded = "succeeded"
    failed = "failed"


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    review_id: Mapped[int] = mapped_column(
        ForeignKey("reviews.id", ondelete="CASCADE"), unique=True, index=True
    )
    category_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    auto_category_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("auto_categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    sentiment: Mapped[Optional[Sentiment]] = mapped_column(SAEnum(Sentiment), nullable=True)
    sentiment_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    analyzed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    status: Mapped[AnalysisStatus] = mapped_column(
        SAEnum(AnalysisStatus), default=AnalysisStatus.succeeded
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    review: Mapped["Review"] = relationship(back_populates="analysis")
    category: Mapped[Optional["Category"]] = relationship()


class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
