"""Initial PostgreSQL-backed Viewer schema.

Revision ID: 0001_viewer_pg
Revises:
Create Date: 2026-08-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0001_viewer_pg"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "viewer_tournaments",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("public_view_token", sa.Text(), nullable=False),
        sa.Column("viewer_sync_key", sa.Text(), nullable=False),
        sa.Column("tournament_name", sa.Text(), nullable=True),
        sa.Column("td_name", sa.Text(), nullable=True),
        sa.Column("tournament_status", sa.Text(), nullable=True),
        sa.Column("latest_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("viewer_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("license_status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("license_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_view_token", name="uq_viewer_tournaments_public_view_token"),
    )


def downgrade() -> None:
    op.drop_table("viewer_tournaments")
