"""competitive_v2_cards: bottom-up success-factor clustering

New table for /competitive-v2 — the v2 page takes a CSV from /vendors
(same shape as v1) but, instead of asking the user for factors, it
asks Claude to CLUSTER the per-strength reasons into ~5 success-
factor categories that emerge from the data itself.

Schema is intentionally simpler than v1 — no factors/criteria input,
just the CSV snapshot and the clustering result payload.

Idempotent: skips work that's already been done.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0019_competitive_v2_cards"
down_revision = "0018_competitive_multi_factors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "competitive_v2_cards" in insp.get_table_names():
        return
    op.create_table(
        "competitive_v2_cards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("model_used", sa.String(length=100), nullable=True),
        sa.Column("input_csv", sa.JSON(), nullable=False),
        sa.Column("result_payload", sa.JSON(), nullable=False),
        sa.Column(
            "hidden",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "display_order",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "competitive_v2_cards" not in insp.get_table_names():
        return
    op.drop_table("competitive_v2_cards")
