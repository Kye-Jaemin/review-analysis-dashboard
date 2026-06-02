"""vendor_reason_cards: persistent /vendors strength-cause / weakness-cause analyses

Each row caches an LLM "why did users feel this way about category X
for vendor Y?" analysis so the modal can reload without another Claude
call every time the user re-opens it.

Idempotent: skips create when the table already exists, matching
0008–0015.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0016_vendor_reason_cards"
down_revision = "0015_competitive_factor_cards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "vendor_reason_cards" in insp.get_table_names():
        return
    op.create_table(
        "vendor_reason_cards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("vendor_key", sa.String(length=100), nullable=False),
        sa.Column("vendor_display", sa.String(length=200), nullable=False),
        sa.Column("category_name", sa.String(length=200), nullable=False),
        sa.Column("band", sa.String(length=20), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("model_used", sa.String(length=100), nullable=True),
        sa.Column(
            "sample_size",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("source_ids_snapshot", sa.JSON(), nullable=False),
        # [{"reason": str, "count": int, "examples": [str, ...]}, ...]
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column(
            "hidden",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
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
    # Composite lookup index — the dashboard reads "do we have a card
    # for this (vendor, category, band)?" for the 📌 saved-indicator
    # on every /vendors page load, so make that hit a single index.
    op.create_index(
        "ix_vendor_reason_cards_lookup",
        "vendor_reason_cards",
        ["vendor_key", "category_name", "band"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "vendor_reason_cards" not in insp.get_table_names():
        return
    op.drop_index(
        "ix_vendor_reason_cards_lookup",
        table_name="vendor_reason_cards",
    )
    op.drop_table("vendor_reason_cards")
