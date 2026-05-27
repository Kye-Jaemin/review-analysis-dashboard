"""theme snapshots → owned by investigations; wipe legacy rows

Revision ID: 0004_snapshots_to_investigations
Revises: 0003_investigations
Create Date: 2026-05-27 00:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = "0004_snapshots_to_investigations"
down_revision = "0003_investigations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Per user request: discard every previously-saved mind map. From now on
    # snapshots belong to an Investigation card. Wiping first so the new
    # FK column doesn't have to reconcile orphaned rows.
    op.execute("DELETE FROM theme_snapshots")
    op.add_column(
        "theme_snapshots",
        sa.Column(
            "investigation_id",
            sa.Integer(),
            sa.ForeignKey("investigations.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("theme_snapshots", "investigation_id")
