"""review_manual_categories junction so duplicate manual cards survive

Background
----------
Same shape of bug we fixed for auto categories in migration 0009: when
the user creates two manual investigation cards over the same source
set with different root scopes (or even the same root re-classified
later), each manual analysis run overwrites Analysis.category_id with
the leaf chosen for THAT run. The previously-classified card's
dashboard then silently zeroes out.

Fix: store the per-card manual tag in a junction table keyed by
(review_id, investigation_id). Each card owns its own row per review,
so re-running on one card touches nothing belonging to a sibling.

Migration steps
---------------
1. Create the junction table with cascading FKs on review + investigation
   so a deleted review / card automatically cleans up its links.
2. Backfill from existing Analysis rows: for each Analysis row with
   category_id set, attach it to every Investigation card whose
   root_ids tree contains that category. This preserves the most-recent
   classification for whichever card was analyzed last, and copies it
   into any other card that targets the same subtree — better than
   leaving them empty.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0012_manual_category_junction"
down_revision = "0011_investigation_display_order"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing = set(insp.get_table_names())
    if "review_manual_categories" not in existing:
        op.create_table(
            "review_manual_categories",
            sa.Column(
                "review_id",
                sa.Integer(),
                sa.ForeignKey("reviews.id", ondelete="CASCADE"),
                primary_key=True,
                nullable=False,
            ),
            sa.Column(
                "investigation_id",
                sa.Integer(),
                sa.ForeignKey("investigations.id", ondelete="CASCADE"),
                primary_key=True,
                nullable=False,
            ),
            sa.Column(
                "category_id",
                sa.Integer(),
                sa.ForeignKey("categories.id", ondelete="CASCADE"),
                nullable=False,
            ),
        )
        op.create_index(
            "ix_review_manual_categories_category_id",
            "review_manual_categories",
            ["category_id"],
        )
        op.create_index(
            "ix_review_manual_categories_investigation_id",
            "review_manual_categories",
            ["investigation_id"],
        )

    # Backfill from existing Analysis.category_id: for each classified
    # Analysis row, attach it to every Investigation whose root_ids tree
    # contains that category. Done in Python (not raw SQL) so we can walk
    # the parent_id chain to test ancestry — root_ids on Investigation is
    # a JSON int array, not relational, and CTEs on JSON paths get gnarly
    # across both Postgres and SQLite. We're working with low thousands of
    # rows in practice; the cost is fine for a one-shot migration.
    conn = bind
    cats_rows = list(conn.execute(sa.text("SELECT id, parent_id FROM categories")))
    parent_by_id = {int(r[0]): (int(r[1]) if r[1] is not None else None) for r in cats_rows}

    def ancestors(cid: int):
        seen = set()
        while cid is not None and cid not in seen:
            seen.add(cid)
            yield cid
            cid = parent_by_id.get(cid)

    invs = list(conn.execute(sa.text("SELECT id, root_ids FROM investigations")))
    inv_roots = []
    import json as _json
    for inv_id, raw in invs:
        try:
            roots = raw if isinstance(raw, list) else _json.loads(raw or "[]")
        except Exception:
            roots = []
        roots = [int(r) for r in (roots or []) if r is not None]
        if roots:
            inv_roots.append((int(inv_id), set(roots)))

    if inv_roots:
        analyses = list(conn.execute(sa.text(
            "SELECT review_id, category_id FROM analyses WHERE category_id IS NOT NULL"
        )))
        rows_to_insert: list[dict] = []
        seen_pairs: set[tuple[int, int]] = set()
        for review_id, category_id in analyses:
            rid = int(review_id)
            cid = int(category_id)
            anc = set(ancestors(cid))
            for inv_id, roots in inv_roots:
                if anc & roots:
                    key = (rid, inv_id)
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    rows_to_insert.append({
                        "review_id": rid,
                        "investigation_id": inv_id,
                        "category_id": cid,
                    })
        if rows_to_insert:
            # Avoid duplicates if a previous partial run inserted some.
            op.bulk_insert(
                sa.table(
                    "review_manual_categories",
                    sa.column("review_id", sa.Integer),
                    sa.column("investigation_id", sa.Integer),
                    sa.column("category_id", sa.Integer),
                ),
                rows_to_insert,
            )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "review_manual_categories" in set(insp.get_table_names()):
        op.drop_index("ix_review_manual_categories_investigation_id", "review_manual_categories")
        op.drop_index("ix_review_manual_categories_category_id", "review_manual_categories")
        op.drop_table("review_manual_categories")
