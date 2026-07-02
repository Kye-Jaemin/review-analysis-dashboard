"""vendor_categories: user-named groups of Investigation cards

New table backing the dashboard's "카테고리 카드" feature — lets the
user group several existing Investigation cards (e.g. several fitness
apps) under a category label (e.g. "헬스"), then scope /vendors to just
that group via ?vendor_category_id=.

Membership (investigation_ids) is a plain JSON list, not a foreign key
— same soft-reference pattern Investigation itself uses for
source_ids/root_ids. Schema mirrors investigations (label/description/
display_order/hidden/timestamps).

Idempotent: skips work that's already been done.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0022_vendor_categories"
down_revision = "0021_v3_criteria_mapping"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "vendor_categories" in insp.get_table_names():
        return
    op.create_table(
        "vendor_categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("investigation_ids", sa.JSON(), nullable=False),
        sa.Column(
            "display_order",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
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


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "vendor_categories" not in insp.get_table_names():
        return
    op.drop_table("vendor_categories")
