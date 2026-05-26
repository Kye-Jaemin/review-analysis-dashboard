"""initial schema

Revision ID: 0001_init
Revises:
Create Date: 2026-05-26 00:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None


source_type = sa.Enum("google_play", "app_store", "reddit", "web", name="sourcetype")
collection_status = sa.Enum("pending", "running", "succeeded", "failed", name="collectionstatus")
sentiment_enum = sa.Enum(
    "very_positive", "positive", "neutral", "negative", "very_negative", name="sentiment"
)
analysis_status = sa.Enum("succeeded", "failed", name="analysisstatus")


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("type", source_type, nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("display_name", sa.String(300), nullable=True),
        sa.Column("icon_url", sa.String(500), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "collection_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id", ondelete="CASCADE")),
        sa.Column("status", collection_status, nullable=False, server_default="pending"),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("fetched_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("new_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
    )

    op.create_table(
        "reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id", ondelete="CASCADE"), index=True),
        sa.Column("external_id", sa.String(255), nullable=False, index=True),
        sa.Column("author", sa.String(255), nullable=True),
        sa.Column("posted_at", sa.DateTime(), nullable=True, index=True),
        sa.Column("rating", sa.Float(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("url", sa.String(1000), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=True),
        sa.Column("collected_at", sa.DateTime(), nullable=False, server_default=sa.func.now(), index=True),
        sa.UniqueConstraint("source_id", "external_id", name="uq_review_source_extid"),
    )

    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("categories.id", ondelete="CASCADE"), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("path", sa.String(800), nullable=False, server_default="", index=True),
    )

    op.create_table(
        "analyses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("review_id", sa.Integer(), sa.ForeignKey("reviews.id", ondelete="CASCADE"), unique=True, index=True),
        sa.Column("category_id", sa.Integer(), sa.ForeignKey("categories.id", ondelete="SET NULL"), nullable=True),
        sa.Column("sentiment", sentiment_enum, nullable=True),
        sa.Column("sentiment_score", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("analyzed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("status", analysis_status, nullable=False, server_default="succeeded"),
        sa.Column("error", sa.Text(), nullable=True),
    )

    op.create_table(
        "analysis_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("processed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("analysis_jobs")
    op.drop_table("analyses")
    op.drop_table("categories")
    op.drop_table("reviews")
    op.drop_table("collection_jobs")
    op.drop_table("sources")
    sa.Enum(name="analysisstatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="sentiment").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="collectionstatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="sourcetype").drop(op.get_bind(), checkfirst=True)
