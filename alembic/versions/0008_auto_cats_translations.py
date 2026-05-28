"""auto_categories: language + translations cache

Revision ID: 0008_auto_cats_translations
Revises: 0007_snapshots_auto_cats
Create Date: 2026-05-28 00:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = "0008_auto_cats_translations"
down_revision = "0007_snapshots_auto_cats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "auto_categories",
        sa.Column("language", sa.String(10), nullable=False, server_default="en"),
    )
    op.add_column(
        "auto_categories",
        sa.Column("translations", sa.JSON(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("auto_categories", "translations")
    op.drop_column("auto_categories", "language")
