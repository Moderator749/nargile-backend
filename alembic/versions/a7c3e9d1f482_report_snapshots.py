"""Reports: product cost with its currency; an order's group and a line's VAT and cost, as sold

product_variants.cost_price widens to four decimals (a worksheet cost may carry
them) and gains cost_currency, TRY until a worksheet says otherwise.

orders.segment_code and order_items.vat_rate / unit_cost are frozen at checkout
from now on. Existing orders get the group their account is in today (Perakende
when it has none, as checkout prices it) and each line the VAT rate of its
price row; their cost stays null — nobody recorded it — and the reports
estimate it from the product's current cost.

Revision ID: a7c3e9d1f482
Revises: e9c4b7a2d315
Create Date: 2026-11-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7c3e9d1f482"
down_revision: Union[str, None] = "e9c4b7a2d315"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "product_variants", "cost_price",
        type_=sa.Numeric(12, 4), existing_type=sa.Numeric(12, 2), existing_nullable=True,
    )
    op.add_column(
        "product_variants",
        sa.Column(
            "cost_currency", postgresql.ENUM(name="currencytype", create_type=False),
            nullable=False, server_default="TRY",
        ),
    )
    op.add_column(
        "orders", sa.Column("segment_code", postgresql.ENUM(name="segmenttype", create_type=False), nullable=True)
    )
    op.add_column("order_items", sa.Column("vat_rate", sa.Numeric(4, 2), nullable=True))
    op.add_column("order_items", sa.Column("unit_cost", sa.Numeric(12, 4), nullable=True))

    op.execute(
        """
        UPDATE orders o
           SET segment_code = COALESCE(s.code, 'RETAIL')
          FROM users u
          LEFT JOIN customer_segments s ON s.id = u.segment_id
         WHERE u.id = o.user_id
        """
    )
    # The buyer's own list first, then a TRY row: the row checkout would pick.
    op.execute(
        """
        UPDATE order_items oi
           SET vat_rate = picked.vat_rate
          FROM (
                SELECT DISTINCT ON (i.id) i.id, p.vat_rate
                  FROM order_items i
                  JOIN orders o ON o.id = i.order_id
                  JOIN users u ON u.id = o.user_id
                  JOIN variant_prices p ON p.variant_id = i.variant_id
                 ORDER BY i.id, (p.segment_id = COALESCE(u.segment_id, 1)) DESC, (p.currency = 'TRY') DESC
               ) picked
         WHERE picked.id = oi.id
        """
    )


def downgrade() -> None:
    op.drop_column("order_items", "unit_cost")
    op.drop_column("order_items", "vat_rate")
    op.drop_column("orders", "segment_code")
    op.drop_column("product_variants", "cost_currency")
    op.alter_column(
        "product_variants", "cost_price",
        type_=sa.Numeric(12, 2), existing_type=sa.Numeric(12, 4), existing_nullable=True,
    )
