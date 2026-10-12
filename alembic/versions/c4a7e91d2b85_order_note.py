"""Sipariş Notu: the customer's own message on an order

Optional free text from checkout — a delivery window, a missing invoice
detail, a request — kept as typed and read by whoever picks the order up in
the panel. Nothing in the system acts on it.

Revision ID: c4a7e91d2b85
Revises: b7e3c25a9f41
Create Date: 2026-10-09
"""

import sqlalchemy as sa
from alembic import op

revision = "c4a7e91d2b85"
down_revision = "b7e3c25a9f41"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("order_note", sa.Text(), nullable=True))


def downgrade() -> None:
    # Lossy: the notes customers wrote go with the column.
    op.drop_column("orders", "order_note")
