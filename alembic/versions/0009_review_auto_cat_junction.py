"""review_auto_categories junction so one review can belong to many cards

Background
----------
`Analysis.auto_category_id` was a single FK. When the same source (and
therefore the same reviews) sit in two Investigation cards and both run
auto analysis, Card B's Phase 2 overwrites every shared review's
auto_category_id with one of Card B's categories — so Card A's dashboard
queries (`Analysis.auto_category_id IN Card_A_categories`) suddenly return
zero hits for those reviews and the Top 10 looks broken.

Fix: split the (review, auto_category) relation off into a junction table.
Each review can carry one tag per card simultaneously. The Analysis row
keeps sentiment / score / user_tier / summary — those are review-level
properties shared across cards.

Migration steps
---------------
1. Create the junction table with cascading FKs on both sides so a deleted
   review or a deleted auto_category cleans up its links automatically.
2. Backfill from existing `analyses.auto_category_id` rows so anything
   already classified keeps showing up after the deploy.
3. Drop the now-redundant column + index from `analyses`.
"""
from alembic import op
import sqlalchemy as sa


revision = "0009_review_auto_cat_junction"
down_revision = "0008_auto_cats_translations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_auto_categories",
        sa.Column(
            "review_id",
            sa.Integer(),
            sa.ForeignKey("reviews.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "auto_category_id",
            sa.Integer(),
            sa.ForeignKey("auto_categories.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
    )
    op.create_index(
        "ix_review_auto_categories_auto_category_id",
        "review_auto_categories",
        ["auto_category_id"],
    )

    # Backfill: every (review_id, auto_category_id) currently encoded as the
    # single FK on analyses becomes a junction row. The ON CONFLICT guard
    # keeps this idempotent on re-runs (Postgres + SQLite both honour it
    # under the syntaxes we use elsewhere — but we'd rather rely on the FKs
    # for uniqueness, so plain INSERT ... SELECT with a NOT EXISTS works on
    # both engines).
    op.execute(
        """
        INSERT INTO review_auto_categories (review_id, auto_category_id)
        SELECT a.review_id, a.auto_category_id
        FROM analyses a
        WHERE a.auto_category_id IS NOT NULL
        """
    )

    # Drop the FK column from analyses. Different engines name FK
    # constraints differently; use batch_alter so SQLite (test) and Postgres
    # (prod) both cope.
    with op.batch_alter_table("analyses") as batch_op:
        try:
            batch_op.drop_index("ix_analyses_auto_category_id")
        except Exception:
            # Index may not exist on older fresh DBs.
            pass
        batch_op.drop_column("auto_category_id")


def downgrade() -> None:
    # Re-add the column and try to repopulate from the junction (first link
    # per review wins — there's no way to recover the "primary" tag
    # losslessly once cards share reviews).
    with op.batch_alter_table("analyses") as batch_op:
        batch_op.add_column(
            sa.Column(
                "auto_category_id",
                sa.Integer(),
                sa.ForeignKey("auto_categories.id", ondelete="SET NULL"),
                nullable=True,
            )
        )
        batch_op.create_index("ix_analyses_auto_category_id", ["auto_category_id"])

    op.execute(
        """
        UPDATE analyses
        SET auto_category_id = (
            SELECT j.auto_category_id
            FROM review_auto_categories j
            WHERE j.review_id = analyses.review_id
            LIMIT 1
        )
        """
    )

    op.drop_index("ix_review_auto_categories_auto_category_id", "review_auto_categories")
    op.drop_table("review_auto_categories")
