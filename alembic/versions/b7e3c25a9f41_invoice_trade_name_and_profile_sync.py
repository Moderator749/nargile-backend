"""Firma adı on the invoice, and room to save an edited address line

"Faturayı firmaya istiyorum" now also asks what the firm is called day to day
("Firma Adı"), beside the registered ünvan: it goes onto the order's invoice
snapshot and onto the customer's profile, so the next order starts filled in.

``customer_addresses.street`` grows to 200 characters because the invoice
address is edited at checkout as one free-text line, and that line is what the
column then holds.

Revision ID: b7e3c25a9f41
Revises: a2f9d3b78c14
Create Date: 2026-10-09
"""

import sqlalchemy as sa
from alembic import op

revision = "b7e3c25a9f41"
down_revision = "a2f9d3b78c14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("invoice_trade_name", sa.String(length=200), nullable=True))
    op.add_column("customer_profiles", sa.Column("trade_name", sa.String(length=200), nullable=True))
    op.alter_column(
        "customer_addresses",
        "street",
        existing_type=sa.String(length=120),
        type_=sa.String(length=200),
        existing_nullable=True,
    )


def downgrade() -> None:
    # Lossy both ways: the trade names go, and an address line over 120
    # characters is cut to fit the narrower column.
    op.execute("UPDATE customer_addresses SET street = left(street, 120) WHERE length(street) > 120")
    op.alter_column(
        "customer_addresses",
        "street",
        existing_type=sa.String(length=200),
        type_=sa.String(length=120),
        existing_nullable=True,
    )
    op.drop_column("customer_profiles", "trade_name")
    op.drop_column("orders", "invoice_trade_name")
