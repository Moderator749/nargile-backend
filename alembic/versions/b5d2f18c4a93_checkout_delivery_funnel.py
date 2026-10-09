"""The checkout delivery funnel: who collects it, and how it travels

Checkout now asks two things it did not: how the order should travel, from a set
that depends on what kind of account is buying, and who signs for it when that
is not the customer ("Başka biri teslim alacak").

Shipping methods gain a branch collection for bireysel, a nakliye ambarı for
firms and an export option for abroad; the İstanbul courier stays. The receiver
is two columns on the order rather than a row anywhere: it belongs to one
shipment, like the address beside it.

Revision ID: b5d2f18c4a93
Revises: a4c91e7d5b82
Create Date: 2026-10-08
"""

import sqlalchemy as sa
from alembic import op

revision = "b5d2f18c4a93"
down_revision = "a4c91e7d5b82"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE shippingmethod ADD VALUE 'CARGO_BRANCH'")
    op.execute("ALTER TYPE shippingmethod ADD VALUE 'FREIGHT'")
    op.execute("ALTER TYPE shippingmethod ADD VALUE 'INTERNATIONAL'")
    op.add_column("orders", sa.Column("receiver_name", sa.String(length=150), nullable=True))
    op.add_column("orders", sa.Column("receiver_phone", sa.String(length=30), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "receiver_phone")
    op.drop_column("orders", "receiver_name")

    # Orders already placed one of the new ways keep travelling, as plain cargo:
    # the shipment is real whatever the type can name it.
    op.execute(
        "UPDATE orders SET shipping_method = 'CARGO'"
        " WHERE shipping_method IN ('CARGO_BRANCH', 'FREIGHT', 'INTERNATIONAL')"
    )
    op.execute("ALTER TYPE shippingmethod RENAME TO shippingmethod_old")
    op.execute("CREATE TYPE shippingmethod AS ENUM ('CARGO', 'COURIER_ISTANBUL')")
    op.execute(
        "ALTER TABLE orders ALTER COLUMN shipping_method DROP DEFAULT,"
        " ALTER COLUMN shipping_method TYPE shippingmethod"
        " USING shipping_method::text::shippingmethod,"
        " ALTER COLUMN shipping_method SET DEFAULT 'CARGO'"
    )
    op.execute("DROP TYPE shippingmethod_old")
