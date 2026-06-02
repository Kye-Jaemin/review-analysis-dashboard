"""competitive_factor_cards: multi-factor support

Adds a `factors` JSON column to store the FULL list of competitive
factors the user submitted in one analysis. The legacy `factor`
column stays as the single source of truth for the first factor (so
old code paths keep working) and the sidebar's display label, but
new code reads `factors` and falls back to `[factor]` for backward
compatibility.

The new flow also bundles a per-factor result grouping inside
`result_rows`, so result_rows becomes `{groups: [{factor, rows}, ...]}`
instead of a flat list. That's a JSON shape change, not a schema
change, so no migration is needed for it — the service layer
tolerates both old and new shapes on read.

Idempotent: skips work that's already been done.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0018_competitive_multi_factors"
down_revision = "0017_competitive_csv_schema"
branch_labels = None
depends_on = None


def _columns(insp, table: str) -> set[str]:
    try:
        return {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return set()


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "competitive_factor_cards" not in insp.get_table_names():
        return
    cols = _columns(insp, "competitive_factor_cards")
    if "factors" not in cols:
        op.add_column(
            "competitive_factor_cards",
            sa.Column("factors", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "competitive_factor_cards" not in insp.get_table_names():
        return
    cols = _columns(insp, "competitive_factor_cards")
    if "factors" in cols:
        op.drop_column("competitive_factor_cards", "factors")
