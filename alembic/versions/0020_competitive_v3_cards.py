"""competitive_v3_cards: LLM-categorized vendor analysis snapshots

New table for /competitive-v3 — stores both the input rows (enriched
with the LLM's 카테고리 assignment) and the build_categorized_view
output. Loading a saved card replays the categorized view without a
Claude call; re-exporting the categorized XLSX uses the stored rows.

Schema mirrors competitive_v2_cards (label + payload), plus an extra
input_filename column for display in the saved-card list.

Idempotent: skips work that's already been done.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0020_competitive_v3_cards"
down_revision = "0019_competitive_v2_cards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "competitive_v3_cards" in insp.get_table_names():
        return
    op.create_table(
        "competitive_v3_cards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("model_used", sa.String(length=100), nullable=True),
        sa.Column("input_filename", sa.String(length=255), nullable=True),
        sa.Column("input_rows", sa.JSON(), nullable=False),
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
    if "competitive_v3_cards" not in insp.get_table_names():
        return
    op.drop_table("competitive_v3_cards")
