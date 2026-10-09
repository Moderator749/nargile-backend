"""Shipping methods under their own names, and what each one needs

The methods are renamed to what the shop calls them — YURTICI_DOOR,
YURTICI_BRANCH, NAKLIYE_AMBARI, ISTANBUL_FREE, INTERNATIONAL_FREIGHT — rather
than the generic CARGO/FREIGHT they started as. Renaming enum values keeps every
order pointing at the same method it already had; nothing is rewritten.

The İstanbul option changes meaning with its name: it was a same-day courier the
buyer paid on delivery, and is now the shop's own free İstanbul run, offered to
a firm whose registered address is there. Orders placed under the old meaning
keep the payer they were recorded with.

Three columns come with the new options: a firm's Yurtiçi agreement number, and
the nakliye ambarı it wants the goods handed to.

Revision ID: d7f4b16e8c52
Revises: c3e8b94f6d27
Create Date: 2026-10-09
"""

import sqlalchemy as sa
from alembic import op

revision = "d7f4b16e8c52"
down_revision = "c3e8b94f6d27"
branch_labels = None
depends_on = None

RENAMES = (
    ("CARGO", "YURTICI_DOOR"),
    ("CARGO_BRANCH", "YURTICI_BRANCH"),
    ("FREIGHT", "NAKLIYE_AMBARI"),
    ("INTERNATIONAL", "INTERNATIONAL_FREIGHT"),
    ("COURIER_ISTANBUL", "ISTANBUL_FREE"),
)


def _rename(pairs) -> None:
    for old, new in pairs:
        op.execute(f"ALTER TYPE shippingmethod RENAME VALUE '{old}' TO '{new}'")


def upgrade() -> None:
    _rename(RENAMES)
    # The column default is a constant of the type, so it is re-stated under the
    # new name rather than left to resolve.
    op.execute("ALTER TABLE orders ALTER COLUMN shipping_method SET DEFAULT 'YURTICI_DOOR'")

    op.add_column("orders", sa.Column("shipping_contract_no", sa.String(length=50), nullable=True))
    op.add_column("orders", sa.Column("freight_company_name", sa.String(length=150), nullable=True))
    op.add_column("orders", sa.Column("freight_phone", sa.String(length=30), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "freight_phone")
    op.drop_column("orders", "freight_company_name")
    op.drop_column("orders", "shipping_contract_no")
    _rename([(new, old) for old, new in RENAMES])
    op.execute("ALTER TABLE orders ALTER COLUMN shipping_method SET DEFAULT 'CARGO'")
