"""analyses.user_tier — beta paid/free segmentation

Revision ID: 0006_analysis_user_tier
Revises: 0005_auto_categories
Create Date: 2026-05-28 00:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = "0006_analysis_user_tier"
down_revision = "0005_auto_categories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("analyses", sa.Column("user_tier", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("analyses", "user_tier")
