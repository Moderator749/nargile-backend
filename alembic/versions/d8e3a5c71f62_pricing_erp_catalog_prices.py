"""Pricing worksheet rows: ERP prices and the catalogue's VAT display.

"ERP ve Katalog Fiyatları" under the worksheet: the product's ERP price in
USD, EUR and TL (before VAT), which the price catalogue shows in place of the
sheet's price, and, per customer group, whether the catalogue shows its price
with or without VAT. Every existing row starts with none of them: its
catalogue reads as before.

Revision ID: d8e3a5c71f62
Revises: c3f9a1e7b204
Create Date: 2026-11-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d8e3a5c71f62"
down_revision: Union[str, None] = "c3f9a1e7b204"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PRICES = ("erp_price_usd", "erp_price_eur", "erp_price_tl")
DISPLAYS = ("erp_display_bayi", "erp_display_toptan", "erp_display_kafe", "erp_display_perakende")


def upgrade() -> None:
    for name in PRICES:
        op.add_column("pricing_records", sa.Column(name, sa.Numeric(12, 2), nullable=True))
    for name in DISPLAYS:
        op.add_column("pricing_records", sa.Column(name, sa.Boolean(), nullable=True))


def downgrade() -> None:
    for name in reversed(PRICES + DISPLAYS):
        op.drop_column("pricing_records", name)
