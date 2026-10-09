"""The firm's billing address, frozen onto the order

"Faturayı firmaya/şirkete istiyorum" now also asks where that firm is
registered, because an invoice has to show it. The order keeps its own copy the
way it keeps the delivery address: a document about the day it was issued, not
a window onto the account as it looks later.

Revision ID: a2f9d3b78c14
Revises: f1c84d6a37b9
Create Date: 2026-10-09
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a2f9d3b78c14"
down_revision = "f1c84d6a37b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("invoice_address", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    # Lossy: the addresses those invoices were issued at go with the column.
    op.drop_column("orders", "invoice_address")
