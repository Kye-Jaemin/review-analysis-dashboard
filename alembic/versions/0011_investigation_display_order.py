"""investigations: display_order column for user-controlled card sort

Users want to drag-reorder their investigation cards on the dashboard.
Up to now we surfaced rows ordered by updated_at desc, which is a poor
fit for "I always want my main card at the top" — clicking another card
silently bumps it ahead.

Approach: add a `display_order` integer column. The list endpoint sorts
by it ASC (then updated_at DESC as a stable tiebreaker). Reorder API
writes the new positions transactionally.

Backfill: seed existing rows with display_order = ROW_NUMBER() over
the current updated_at ordering, so the on-screen ordering doesn't
shift the moment this migration runs.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0011_investigation_display_order"
down_revision = "0010_restore_auto_category_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    cols = {c["name"] for c in insp.get_columns("investigations")}
    if "display_order" not in cols:
        op.add_column(
            "investigations",
            sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        )
        # Drop the server default once column exists — Python-side default
        # on the model is what we want going forward (new rows get max+1
        # in the route layer).
        with op.batch_alter_table("investigations") as batch_op:
            batch_op.alter_column("display_order", server_default=None)

    # Backfill so the on-screen ordering stays the same right after deploy.
    # Postgres + SQLite both support a correlated subquery, but
    # ROW_NUMBER() is portable and cleaner. SQLite gained window functions
    # in 3.25 (2018) — fine for any modern environment.
    dialect = bind.dialect.name
    if dialect in ("postgresql", "sqlite"):
        op.execute(
            """
            WITH ranked AS (
              SELECT id,
                     ROW_NUMBER() OVER (ORDER BY updated_at DESC, id DESC) AS rn
              FROM investigations
            )
            UPDATE investigations
            SET display_order = ranked.rn
            FROM ranked
            WHERE investigations.id = ranked.id
            """ if dialect == "postgresql" else
            """
            UPDATE investigations
            SET display_order = (
              SELECT rn FROM (
                SELECT id, ROW_NUMBER() OVER (ORDER BY updated_at DESC, id DESC) AS rn
                FROM investigations
              ) ranked WHERE ranked.id = investigations.id
            )
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    cols = {c["name"] for c in insp.get_columns("investigations")}
    if "display_order" in cols:
        op.drop_column("investigations", "display_order")
