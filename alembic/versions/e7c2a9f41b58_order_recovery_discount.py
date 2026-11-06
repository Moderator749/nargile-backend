"""Orders: the "Kurtarma İndirimi" a failed card order took (``recovery_discount``).

"WhatsApp ile Kurtar" turns a PAYMENT_FAILED card order into a bank transfer
at 5% off. The amount is also in ``discount_amount`` (every total keeps
reading that); this column says how much of it was the recovery's. 0.00 on
every other order.

Revision ID: e7c2a9f41b58
Revises: d3a9e5c71f20
Create Date: 2026-11-06
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e7c2a9f41b58"
down_revision: Union[str, None] = "d3a9e5c71f20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("recovery_discount", sa.Numeric(12, 2), nullable=False, server_default="0.00"),
    )


def downgrade() -> None:
    op.drop_column("orders", "recovery_discount")
