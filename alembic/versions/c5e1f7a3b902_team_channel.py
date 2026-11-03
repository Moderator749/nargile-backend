"""The panel team's channel: admin_messages and admin_message_reads

One shared stream of plain-text messages (a deleted one keeps its row, never
shown again) and, per member, the last message seen. "Günün Notu" lives in
shop_settings and needs no table.

Revision ID: c5e1f7a3b902
Revises: a7c3e9d1f482
Create Date: 2026-11-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c5e1f7a3b902"
down_revision: Union[str, None] = "a7c3e9d1f482"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "admin_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("sender_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("sender_name", sa.String(length=150), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_admin_messages_sender_created", "admin_messages", ["sender_id", "created_at"])
    op.create_index("ix_admin_messages_deleted_at", "admin_messages", ["deleted_at"])
    op.create_table(
        "admin_message_reads",
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("last_read_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("admin_message_reads")
    op.drop_index("ix_admin_messages_deleted_at", table_name="admin_messages")
    op.drop_index("ix_admin_messages_sender_created", table_name="admin_messages")
    op.drop_table("admin_messages")
