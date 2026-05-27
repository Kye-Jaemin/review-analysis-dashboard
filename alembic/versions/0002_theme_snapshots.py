"""theme snapshots

Revision ID: 0002_theme_snapshots
Revises: 0001_init
Create Date: 2026-05-27 00:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = "0002_theme_snapshots"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "theme_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("sentiment", sa.String(50), nullable=False),
        sa.Column("source_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("root_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("summary_lang", sa.String(10), nullable=False, server_default="en"),
        sa.Column("sample_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("themes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("theme_snapshots")
