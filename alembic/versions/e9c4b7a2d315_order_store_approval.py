"""Order "Mağaza Onayı": the shop's own release for packing

orders.store_approved_at, store_approved_by (the admin, kept null once that
account is deleted) and store_approved_by_name (who it was, as named then).
Separate from the payment status: no existing order has it.

Revision ID: e9c4b7a2d315
Revises: d6e3a8f1b2c7
Create Date: 2026-10-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e9c4b7a2d315"
down_revision: Union[str, None] = "d6e3a8f1b2c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("store_approved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "orders",
        sa.Column("store_approved_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.add_column("orders", sa.Column("store_approved_by_name", sa.String(length=150), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "store_approved_by_name")
    op.drop_column("orders", "store_approved_by")
    op.drop_column("orders", "store_approved_at")
