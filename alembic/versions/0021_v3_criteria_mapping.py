"""competitive_v3_cards.criteria_mapping: human-curated grouping of AI categories

Adds a nullable JSON column holding the mapping of the v3 AI categories
into higher-level "competitive criteria". Kept separate from
result_payload so it survives the self-heal rebuild that runs when an
older card's payload is missing structured fields.

Idempotent: skips work that's already been done.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0021_v3_criteria_mapping"
down_revision = "0020_competitive_v3_cards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "competitive_v3_cards" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("competitive_v3_cards")}
    if "criteria_mapping" in cols:
        return
    op.add_column(
        "competitive_v3_cards",
        sa.Column("criteria_mapping", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "competitive_v3_cards" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("competitive_v3_cards")}
    if "criteria_mapping" not in cols:
        return
    op.drop_column("competitive_v3_cards", "criteria_mapping")
