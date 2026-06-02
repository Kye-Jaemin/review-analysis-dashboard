"""competitive_factor_cards: persistent /competitive results

New table that snapshots a /competitive analysis (factor + threshold +
LLM result JSON) so the user can reload that ranking without paying
for another Claude completion every time their cache evicts.

No FK out — the result JSON is denormalized on purpose. Vendor rows
inside `result` reference vendors by their string `key` and a list of
`source_ids` valid at scoring time; we don't try to keep those in sync
when the user later adds/removes sources, because the whole point of
the card is "this is what the analysis looked like when I saved it".
If they want a fresh view, the UI offers a re-analyze button.

Idempotent: skips create when the table already exists, matching the
pattern in 0008–0014.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0015_competitive_factor_cards"
down_revision = "0014_junction_sentiment_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "competitive_factor_cards" in insp.get_table_names():
        return
    op.create_table(
        "competitive_factor_cards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("factor", sa.String(length=200), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column(
            "threshold",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0.5"),
        ),
        sa.Column("model_used", sa.String(length=100), nullable=True),
        sa.Column(
            "universe_size",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # JSON for the full rank_vendors_by_factor() output. Postgres
        # stores this as JSONB-equivalent under SQLAlchemy's JSON type
        # on modern PG; SQLite stores as TEXT. Both round-trip cleanly
        # through Python dicts so the model code doesn't care.
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column(
            "hidden",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "display_order",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
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
    # Index on factor for any future "list all snapshots of factor X"
    # query (sidebar grouping, drift comparison, etc.). Cheap to add now
    # and saves a follow-up migration.
    op.create_index(
        "ix_competitive_factor_cards_factor",
        "competitive_factor_cards",
        ["factor"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "competitive_factor_cards" not in insp.get_table_names():
        return
    op.drop_index(
        "ix_competitive_factor_cards_factor",
        table_name="competitive_factor_cards",
    )
    op.drop_table("competitive_factor_cards")
