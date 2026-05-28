"""restore analyses.auto_category_id as a nullable shadow column

Background
----------
Migration 0009 created the review_auto_categories junction and dropped
`analyses.auto_category_id`. That is correct end-state, but in practice
Render's deploy pipeline applies the DB migration during the build phase
*before* the new code starts serving — and if the new container takes
a while to come up (or rolls back), the previously-deployed container
keeps serving requests for a window. That previous container still has
`auto_category_id` declared on its Analysis ORM model, so every
`SELECT * FROM analyses WHERE review_id = ?` includes the column and
PostgreSQL throws `UndefinedColumnError`.

This migration re-adds the column as a plain nullable INTEGER (no FK
constraint — we don't want this column referenced as a real source of
truth anymore). The current code already stops reading/writing it, so
its presence is purely a compatibility shadow for any older container
still serving traffic mid-rollout.

We deliberately do NOT backfill it from the junction. The junction is
the source of truth now; the shadow column just needs to *exist* so
old SELECTs don't crash. Anything that reads it back will see NULL,
which the old code paths already tolerated (the column was always
nullable).

Once a few days have passed and there's no chance of an old container
being live, we can write a follow-up migration to drop the column for
good.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0010_restore_auto_category_id"
down_revision = "0009_review_auto_cat_junction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    cols = {c["name"] for c in insp.get_columns("analyses")}
    if "auto_category_id" not in cols:
        op.add_column(
            "analyses",
            sa.Column("auto_category_id", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    cols = {c["name"] for c in insp.get_columns("analyses")}
    if "auto_category_id" in cols:
        op.drop_column("analyses", "auto_category_id")
