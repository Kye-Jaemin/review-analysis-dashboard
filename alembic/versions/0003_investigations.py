"""investigations

Revision ID: 0003_investigations
Revises: 0002_theme_snapshots
Create Date: 2026-05-27 00:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = "0003_investigations"
down_revision = "0002_theme_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "investigations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("root_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("investigations")
