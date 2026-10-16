"""the admin's "Not Defteri": personal notes

Each note belongs to the admin who wrote it, and goes with their account.

Revision ID: c5e1d9a47f20
Revises: a8d2f6b31c74
Create Date: 2026-10-14
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c5e1d9a47f20"
down_revision = "a8d2f6b31c74"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_notes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("title", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_admin_notes_user_updated", "admin_notes", ["user_id", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_admin_notes_user_updated", table_name="admin_notes")
    op.drop_table("admin_notes")
