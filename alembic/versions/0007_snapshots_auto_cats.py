"""theme_snapshots: auto_category_ids JSON column for per-cat scope

Revision ID: 0007_snapshots_auto_cats
Revises: 0006_analysis_user_tier
Create Date: 2026-05-28 00:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = "0007_snapshots_auto_cats"
down_revision = "0006_analysis_user_tier"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "theme_snapshots",
        sa.Column("auto_category_ids", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("theme_snapshots", "auto_category_ids")
