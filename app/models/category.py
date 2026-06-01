from __future__ import annotations

from typing import List, Optional

from sqlalchemy import Column, ForeignKey, Integer, String, Table, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


# Junction: a review's manual classification is per-card. Two manual
# investigation cards over the same source set used to fight over
# Analysis.category_id (single FK) — running manual analysis on the
# second card wiped the first card's classification. Storing the tag
# scoped to (review_id, investigation_id) lets every card keep its own
# breakdown intact, just like ReviewAutoCategoryLink does for auto cats.
# (review_id, investigation_id) is unique: a card classifies each review
# exactly once, into one leaf of its root subtree.
ReviewManualCategoryLink = Table(
    "review_manual_categories",
    Base.metadata,
    Column(
        "review_id",
        Integer,
        ForeignKey("reviews.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    ),
    Column(
        "investigation_id",
        Integer,
        ForeignKey("investigations.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    ),
    Column(
        "category_id",
        Integer,
        ForeignKey("categories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    # Per-card sentiment snapshot — see migration 0014. Plain strings so
    # we don't have to drag the Postgres ENUM around.
    Column("sentiment", String(50), nullable=True),
    Column("sentiment_score", Integer, nullable=True),
    Column("user_tier", String(20), nullable=True),
)


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    path: Mapped[str] = mapped_column(String(800), nullable=False, default="", index=True)

    parent: Mapped[Optional["Category"]] = relationship(
        "Category", remote_side="Category.id", back_populates="children"
    )
    children: Mapped[List["Category"]] = relationship(
        "Category", back_populates="parent", cascade="all, delete-orphan"
    )

    def is_leaf(self) -> bool:
        return not self.children
