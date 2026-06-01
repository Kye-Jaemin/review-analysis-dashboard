"""junction snapshots: per-card sentiment in review_auto_categories + review_manual_categories

Option B of the per-card analysis refactor: instead of splitting the
Analysis table into one row per (review, card) — which would have
cascaded into 7+ files plus the Review.analysis relationship — we
piggyback per-card sentiment onto the existing junction rows.

Each junction already exists per (review, card) so adding three
columns there gives each card its own sentiment snapshot without
touching the Analysis schema or the templated read paths that depend
on Review.analysis being uselist=False.

What changes:
  - review_auto_categories  + (sentiment, sentiment_score, user_tier)
  - review_manual_categories + (sentiment, sentiment_score, user_tier)

Backfill copies the current Analysis row's sentiment/score/tier into
every existing junction row that points at that review. So clicking a
card right after deploy still shows the same sentiment it did before.

Going forward:
  - Writers populate the snapshot at analysis time (so re-running on
    Card A doesn't change the snapshot Card B already stored).
  - Readers prefer junction.sentiment over Analysis.sentiment when
    they know the active card.
  - Analysis.sentiment stays as the "latest" global value used by
    /reviews, /export, exporter.py — places that don't have a
    per-card context.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0014_junction_sentiment_snapshot"
down_revision = "0013_investigation_hidden"
branch_labels = None
depends_on = None

_SENTIMENT_ENUM_NAME = "sentiment"


def _has_column(insp, table: str, col: str) -> bool:
    try:
        cols = {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return False
    return col in cols


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    # Use String(50) instead of the Sentiment ENUM so we don't depend on
    # the existing analyses.sentiment ENUM type being shared. Postgres
    # ENUM reuse across tables is a hassle (you'd need CREATE TYPE
    # separately or postgresql.ENUM(name='sentiment', create_type=False))
    # and the dashboard reads these as plain strings anyway.
    for table in ("review_auto_categories", "review_manual_categories"):
        if not _has_column(insp, table, "sentiment"):
            op.add_column(table, sa.Column("sentiment", sa.String(50), nullable=True))
        if not _has_column(insp, table, "sentiment_score"):
            op.add_column(table, sa.Column("sentiment_score", sa.Integer(), nullable=True))
        if not _has_column(insp, table, "user_tier"):
            op.add_column(table, sa.Column("user_tier", sa.String(20), nullable=True))

    # Backfill: copy each junction row's matching Analysis row sentiment
    # snapshot into the junction. The current Analysis row IS the
    # latest, so this gives each card a consistent starting point.
    # Done as two UPDATE ... FROM statements (Postgres syntax) and the
    # equivalent correlated-subquery form (SQLite). Skip if any junction
    # row already has sentiment set (idempotent re-run guard).
    already_done_auto = bind.execute(sa.text(
        "SELECT COUNT(*) FROM review_auto_categories WHERE sentiment IS NOT NULL"
    )).scalar() or 0
    already_done_manual = bind.execute(sa.text(
        "SELECT COUNT(*) FROM review_manual_categories WHERE sentiment IS NOT NULL"
    )).scalar() or 0

    dialect = bind.dialect.name
    if dialect == "postgresql":
        if already_done_auto == 0:
            bind.execute(sa.text("""
                UPDATE review_auto_categories AS j
                SET sentiment = a.sentiment,
                    sentiment_score = a.sentiment_score,
                    user_tier = a.user_tier
                FROM analyses AS a
                WHERE a.review_id = j.review_id
            """))
        if already_done_manual == 0:
            bind.execute(sa.text("""
                UPDATE review_manual_categories AS j
                SET sentiment = a.sentiment,
                    sentiment_score = a.sentiment_score,
                    user_tier = a.user_tier
                FROM analyses AS a
                WHERE a.review_id = j.review_id
            """))
    else:
        # SQLite (or anything without UPDATE ... FROM): correlated
        # subqueries. Slower on big tables but the dataset here is small.
        if already_done_auto == 0:
            bind.execute(sa.text("""
                UPDATE review_auto_categories
                SET sentiment = (
                    SELECT a.sentiment FROM analyses a
                    WHERE a.review_id = review_auto_categories.review_id
                    LIMIT 1
                ),
                sentiment_score = (
                    SELECT a.sentiment_score FROM analyses a
                    WHERE a.review_id = review_auto_categories.review_id
                    LIMIT 1
                ),
                user_tier = (
                    SELECT a.user_tier FROM analyses a
                    WHERE a.review_id = review_auto_categories.review_id
                    LIMIT 1
                )
                WHERE sentiment IS NULL
            """))
        if already_done_manual == 0:
            bind.execute(sa.text("""
                UPDATE review_manual_categories
                SET sentiment = (
                    SELECT a.sentiment FROM analyses a
                    WHERE a.review_id = review_manual_categories.review_id
                    LIMIT 1
                ),
                sentiment_score = (
                    SELECT a.sentiment_score FROM analyses a
                    WHERE a.review_id = review_manual_categories.review_id
                    LIMIT 1
                ),
                user_tier = (
                    SELECT a.user_tier FROM analyses a
                    WHERE a.review_id = review_manual_categories.review_id
                    LIMIT 1
                )
                WHERE sentiment IS NULL
            """))


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    for table in ("review_auto_categories", "review_manual_categories"):
        for col in ("user_tier", "sentiment_score", "sentiment"):
            if _has_column(insp, table, col):
                try:
                    op.drop_column(table, col)
                except Exception:
                    pass
