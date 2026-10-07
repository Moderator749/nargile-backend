"""product sheet: per-segment stock quotas, colour, commercial rules, fırsat block

Two changes, both driven by the admin's product sheet ("Ürün Ekle / Düzenle"):

1. Stock is now one quota per segment. Cafe buyers used to draw from RETAIL_STOCK
   and dealers from B2B_STOCK; the new CAFE_STOCK and DEALER_STOCK quotas are
   seeded from those pools so nobody's availability changes on deploy. The quotas
   are independent from here on (the sheet has four boxes), so the seeding
   duplicates numbers rather than splitting them.
2. Products carry the sheet's colour, its five commercial rule flags and its
   "Fırsat" block. The live sale price keeps living in flash_sales, which the
   product form writes from these columns.

Revision ID: 8b2d5f1c4e07
Revises: 7a1c4e9b2d10
Create Date: 2026-10-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8b2d5f1c4e07'
down_revision: Union[str, Sequence[str], None] = '7a1c4e9b2d10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_WAREHOUSES = ("CAFE_STOCK", "DEALER_STOCK")
# Each new quota starts from the pool that segment used to buy from.
SEEDED_FROM = {"CAFE_STOCK": "RETAIL_STOCK", "DEALER_STOCK": "B2B_STOCK"}

FLAGS = (
    ("is_free_shipping", sa.false()),
    ("has_havale_discount", sa.true()),
    ("gives_points", sa.true()),
    ("can_use_points", sa.true()),
    ("is_coal_rule_applied", sa.false()),
    ("is_opportunity", sa.false()),
    ("opp_is_free_shipping", sa.false()),
    ("opp_has_havale_discount", sa.true()),
    ("opp_gives_points", sa.true()),
    ("opp_can_use_points", sa.true()),
)


def upgrade() -> None:
    """Upgrade schema."""
    # A new enum value cannot be used in the transaction that adds it.
    with op.get_context().autocommit_block():
        for value in NEW_WAREHOUSES:
            op.execute(f"ALTER TYPE warehousetype ADD VALUE IF NOT EXISTS '{value}'")

    for new, source in SEEDED_FROM.items():
        op.execute(
            f"""
            INSERT INTO inventory (variant_id, warehouse_type, quantity)
            SELECT variant_id, '{new}', quantity FROM inventory WHERE warehouse_type = '{source}'
            ON CONFLICT (variant_id, warehouse_type) DO NOTHING
            """
        )

    op.add_column("products", sa.Column("color_name", sa.String(length=60), nullable=True))
    for name, default in FLAGS:
        op.add_column("products", sa.Column(name, sa.Boolean(), nullable=False, server_default=default))
    op.add_column("products", sa.Column("opportunity_price", sa.Numeric(12, 2), nullable=True))
    op.add_column("products", sa.Column("opportunity_stock", sa.Integer(), nullable=True))
    op.add_column("products", sa.Column("opportunity_end_date", sa.DateTime(timezone=True), nullable=True))
    op.add_column("products", sa.Column("opportunity_color", sa.String(length=20), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    for name in ("opportunity_color", "opportunity_end_date", "opportunity_stock", "opportunity_price"):
        op.drop_column("products", name)
    for name, _ in reversed(FLAGS):
        op.drop_column("products", name)
    op.drop_column("products", "color_name")
    op.execute("DELETE FROM inventory WHERE warehouse_type IN ('CAFE_STOCK', 'DEALER_STOCK')")
    # The enum values stay: dropping a value from a Postgres enum needs a type rebuild.
