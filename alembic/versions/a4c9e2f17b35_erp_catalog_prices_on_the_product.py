"""ERP and catalogue prices move from the pricing worksheet to the product.

"ERP ve Katalog Fiyatları" leaves the worksheet ("Fiyat Hesaplama Cetveli")
for the product sheet, under its "Fiyat Tablosu": the ERP price in USD, EUR
and TL (before VAT) and, per customer group, whether the price catalogue
shows its price with or without VAT are the product's, not a worksheet row's.

What rows already hold is carried over: each product takes the most recently
saved row that names it (by its code, else a colour's stock code) and has any
of the seven set. Downgrading copies them back onto every row naming it.

Revision ID: a4c9e2f17b35
Revises: d8e3a5c71f62
Create Date: 2026-11-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a4c9e2f17b35"
down_revision: Union[str, None] = "d8e3a5c71f62"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PRICES = ("erp_price_usd", "erp_price_eur", "erp_price_tl")
DISPLAYS = ("erp_display_bayi", "erp_display_toptan", "erp_display_kafe", "erp_display_perakende")
FIELDS = PRICES + DISPLAYS

_COLUMNS = ", ".join(FIELDS)
_SET = ", ".join(f"{f} = r.{f}" for f in FIELDS)
_ANY = " OR ".join(f"{f} IS NOT NULL" for f in FIELDS)
_BACK = ", ".join(f"{f} = p.{f}" for f in FIELDS)
_ANY_ON_PRODUCT = " OR ".join(f"p.{f} IS NOT NULL" for f in FIELDS)
_NONE_ON_PRODUCT = " AND ".join(f"p.{f} IS NULL" for f in FIELDS)
# Per code, the latest saved row with anything in the section.
_LATEST = f"""
    SELECT DISTINCT ON (lower(trim(product_code))) lower(trim(product_code)) AS code, {_COLUMNS}
    FROM pricing_records
    WHERE trim(product_code) <> '' AND ({_ANY})
    ORDER BY lower(trim(product_code)), updated_at DESC
"""


def _add(table: str) -> None:
    for name in PRICES:
        op.add_column(table, sa.Column(name, sa.Numeric(12, 2), nullable=True))
    for name in DISPLAYS:
        op.add_column(table, sa.Column(name, sa.Boolean(), nullable=True))


def _drop(table: str) -> None:
    for name in reversed(FIELDS):
        op.drop_column(table, name)


def upgrade() -> None:
    _add("products")
    # The product's own code first; a colour's stock code for one still unset.
    op.execute(f"UPDATE products p SET {_SET} FROM ({_LATEST}) r WHERE lower(p.base_code) = r.code")
    op.execute(
        f"""
        UPDATE products p SET {_SET}
        FROM ({_LATEST}) r, product_variants v
        WHERE v.product_id = p.id AND lower(v.sku) = r.code AND {_NONE_ON_PRODUCT}
        """
    )
    _drop("pricing_records")


def downgrade() -> None:
    _add("pricing_records")
    op.execute(
        f"""
        UPDATE pricing_records r SET {_BACK}
        FROM products p
        WHERE lower(trim(r.product_code)) = lower(p.base_code) AND ({_ANY_ON_PRODUCT})
        """
    )
    op.execute(
        f"""
        UPDATE pricing_records r SET {_BACK}
        FROM products p, product_variants v
        WHERE v.product_id = p.id AND lower(trim(r.product_code)) = lower(v.sku) AND ({_ANY_ON_PRODUCT})
        """
    )
    _drop("products")
