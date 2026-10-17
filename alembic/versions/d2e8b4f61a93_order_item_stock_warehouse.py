"""The stock quota each order line was taken from

A B2B list (Bayi / Toptan / Kafe) with no quota of its own (0) now buys from
the retail pool, so the quota a line came out of is no longer implied by the
buyer's segment: it is kept on the line, for a cancellation to put the stock
back where it was taken from. Lines from before stay NULL and go back to the
buyer's own quota, as they always did.

Revision ID: d2e8b4f61a93
Revises: c9a1f5e3b724
Create Date: 2026-10-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d2e8b4f61a93"
down_revision = "c9a1f5e3b724"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "order_items",
        sa.Column("stock_warehouse", postgresql.ENUM(name="warehousetype", create_type=False), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("order_items", "stock_warehouse")
