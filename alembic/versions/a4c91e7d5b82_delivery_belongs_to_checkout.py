"""Delivery belongs to checkout, not to sign-up

Sign-up asked where orders should go, who else might sign for them and which
carrier should bring them. All of that is checkout's to ask — once there is an
order to route — and asking it at sign-up put a page of decisions between a
customer and their account.

So the standing delivery preference goes, and with it the address kind that
only that preference ever wrote. What is left in ``customer_addresses`` is
invoice addresses, the account holder's own and the firm's. Orders keep their
own frozen address, which is where delivery has always actually been decided
(``orders.shipping_address``), so nothing about shipping an order changes here.

Revision ID: a4c91e7d5b82
Revises: e8b2f49d6c13
Create Date: 2026-10-08
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a4c91e7d5b82"
down_revision = "e8b2f49d6c13"
branch_labels = None
depends_on = None

# Every value the type had when it was last offered, for the way back.
DELIVERY_MODE_VALUES = (
    "BILLING_ADDRESS",
    "CORPORATE_BILLING_ADDRESS",
    "CARGO_BRANCH",
    "DIFFERENT_ADDRESS",
    "OTHER_RECEIVER",
    "CARGO_TO_BILLING",
    "FREIGHT_TO_BILLING",
    "INTERNATIONAL_CARGO",
    "FREIGHT_FORWARDER",
)


def upgrade() -> None:
    op.drop_column("customer_profiles", "receiver_phone")
    op.drop_column("customer_profiles", "receiver_name")
    op.drop_column("customer_profiles", "delivery_mode")
    op.execute("DROP TYPE deliverymode")

    # Written only by the delivery preference that just went; an invoice
    # address is what this table holds now.
    op.execute("DELETE FROM customer_addresses WHERE kind = 'DELIVERY'")
    op.execute("ALTER TYPE addresskind RENAME TO addresskind_old")
    op.execute("CREATE TYPE addresskind AS ENUM ('BILLING', 'CORPORATE_BILLING')")
    op.execute(
        "ALTER TABLE customer_addresses ALTER COLUMN kind"
        " TYPE addresskind USING kind::text::addresskind"
    )
    op.execute("DROP TYPE addresskind_old")


def downgrade() -> None:
    op.execute("ALTER TYPE addresskind RENAME TO addresskind_old")
    op.execute(
        "CREATE TYPE addresskind AS ENUM ('BILLING', 'CORPORATE_BILLING', 'DELIVERY')"
    )
    op.execute(
        "ALTER TABLE customer_addresses ALTER COLUMN kind"
        " TYPE addresskind USING kind::text::addresskind"
    )
    op.execute("DROP TYPE addresskind_old")

    delivery_mode = postgresql.ENUM(*DELIVERY_MODE_VALUES, name="deliverymode")
    delivery_mode.create(op.get_bind(), checkfirst=False)
    # The preferences themselves are gone for good; the columns come back empty.
    op.add_column("customer_profiles", sa.Column("delivery_mode", delivery_mode, nullable=True))
    op.add_column("customer_profiles", sa.Column("receiver_name", sa.String(length=150), nullable=True))
    op.add_column("customer_profiles", sa.Column("receiver_phone", sa.String(length=20), nullable=True))
