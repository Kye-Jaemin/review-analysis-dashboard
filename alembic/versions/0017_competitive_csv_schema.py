"""competitive_factor_cards: switch to CSV-driven analysis

Major redesign — drop the old "score-all-auto-categories-in-DB"
flow and replace it with a "upload-a-CSV-from-/vendors and classify
its strength rows against the factor" flow. The schema gets two new
JSON columns:

  - input_csv:   the CSV row set the user uploaded (full snapshot
                 so the card stays reproducible even if the
                 underlying DB drifts)
  - result_rows: the rows that survived the LLM relevance filter,
                 with the relevance score attached

We also DROP every existing row from the table — per the user's
decision to start from a clean slate rather than maintain two
shapes in parallel. The old `result` and `universe_size` columns
stay in the schema (now nullable) so the table doesn't need to be
rebuilt; new code writes None/0 there.

Idempotent: skips work that's already been done. Safe to re-run.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0017_competitive_csv_schema"
down_revision = "0016_vendor_reason_cards"
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

    # Wipe legacy rows — user requested clean slate.
    op.execute("DELETE FROM competitive_factor_cards")

    cols = _columns(insp, "competitive_factor_cards")
    if "input_csv" not in cols:
        op.add_column(
            "competitive_factor_cards",
            sa.Column("input_csv", sa.JSON(), nullable=True),
        )
    if "result_rows" not in cols:
        op.add_column(
            "competitive_factor_cards",
            sa.Column("result_rows", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "competitive_factor_cards" not in insp.get_table_names():
        return
    cols = _columns(insp, "competitive_factor_cards")
    if "result_rows" in cols:
        op.drop_column("competitive_factor_cards", "result_rows")
    if "input_csv" in cols:
        op.drop_column("competitive_factor_cards", "input_csv")
