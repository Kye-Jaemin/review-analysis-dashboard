"""investigations: `hidden` flag for dashboard visibility toggle

Users wanted to keep cards in the workspace (analyses, junction rows,
saved mindmaps still useful for re-analysis or vendor compare) but
remove them from the main grid clutter. Add a boolean flag; the list
endpoint defaults to filtering hidden=False, with ?include_hidden=1
for the "show hidden" toggle. Drag-reorder and per-card APIs continue
to work on hidden rows.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0013_investigation_hidden"
down_revision = "0012_manual_category_junction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    cols = {c["name"] for c in insp.get_columns("investigations")}
    if "hidden" not in cols:
        op.add_column(
            "investigations",
            sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        # Drop the server default so future inserts use the Python-side
        # default on the model (= False) without DB-level coupling.
        with op.batch_alter_table("investigations") as batch_op:
            batch_op.alter_column("hidden", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    cols = {c["name"] for c in insp.get_columns("investigations")}
    if "hidden" in cols:
        op.drop_column("investigations", "hidden")
