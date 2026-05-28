"""auto categories — per-investigation LLM-derived top-10 themes

Revision ID: 0005_auto_categories
Revises: 0004_snapshots_to_investigations
Create Date: 2026-05-28 00:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = "0005_auto_categories"
down_revision = "0004_snapshots_to_investigations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auto_categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "investigation_id",
            sa.Integer(),
            sa.ForeignKey("investigations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("review_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_auto_categories_investigation_id", "auto_categories", ["investigation_id"]
    )
    op.add_column(
        "analyses",
        sa.Column(
            "auto_category_id",
            sa.Integer(),
            sa.ForeignKey("auto_categories.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_analyses_auto_category_id", "analyses", ["auto_category_id"])


def downgrade() -> None:
    op.drop_index("ix_analyses_auto_category_id", "analyses")
    op.drop_column("analyses", "auto_category_id")
    op.drop_index("ix_auto_categories_investigation_id", "auto_categories")
    op.drop_table("auto_categories")
