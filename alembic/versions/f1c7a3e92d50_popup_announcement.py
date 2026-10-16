"""the landing popup ("Açılış duyurusu"), beside the special-day banner

One row (id 1): title, message, on/off and a time range. It starts switched
off, so nothing appears on the storefront until an admin publishes it.

Revision ID: f1c7a3e92d50
Revises: e6b3c8f02a71
Create Date: 2026-10-14
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f1c7a3e92d50"
down_revision = "e6b3c8f02a71"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "popup_announcements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("starts_at", sa.DateTime(timezone=True)),
        sa.Column("ends_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
    )


def downgrade() -> None:
    op.drop_table("popup_announcements")
