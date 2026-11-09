"""A product's ERP price per customer group, written from the worksheet's ERP row.

"ERP ve Katalog Fiyatları" kept one ERP price per currency for every group;
the pricing worksheet types one per group (its blue "ERP fiyatı" row: Bayi
105, Kafe 125…). `products.erp_prices` replaces the three columns: per
currency and group, before VAT — {"TRY": {"DEALER": "105.00"}}. A price the
old columns held is carried to all four groups of its currency.

Applying a worksheet now writes its ERP row there; until now it wrote only the
green "uygulanacak" row, so the catalogue priced from that (110.52 for an ERP
of 105). Each product takes the ERP row of the worksheet row last applied to
it (by its code, else a colour's stock code), for a group it has no price for
yet: a class's in the sheet's own VAT basis, retail's after VAT, both brought
to before VAT at the sheet's rate.

Revision ID: c7d2e5a9f041
Revises: a4c9e2f17b35
Create Date: 2026-11-09
"""

import json
from decimal import ROUND_HALF_UP, Decimal
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "c7d2e5a9f041"
down_revision: Union[str, None] = "a4c9e2f17b35"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD = {"TRY": "erp_price_tl", "USD": "erp_price_usd", "EUR": "erp_price_eur"}
GROUPS = ("DEALER", "WHOLESALE", "CAFE", "RETAIL")
CENT = Decimal("0.01")


def _before_vat(price: str, after_vat: bool, rate: Decimal) -> str:
    value = Decimal(price)
    if after_vat:
        value = value / (1 + rate / 100)
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP))


def _sheet_erp(sheet: dict) -> dict[str, str]:
    """A stored worksheet's ERP row per group, before VAT."""
    rate = Decimal(sheet["retail"]["vat_rate"])
    erp = {}
    for col in sheet.get("columns", []):
        if col.get("segment") in GROUPS and col.get("erp_price"):
            erp[col["segment"]] = _before_vat(col["erp_price"], sheet.get("vat_mode") == "INCL", rate)
    if sheet["retail"].get("erp_price"):
        erp["RETAIL"] = _before_vat(sheet["retail"]["erp_price"], True, rate)
    return erp


def upgrade() -> None:
    op.add_column("products", sa.Column("erp_prices", JSONB(), nullable=True))
    bind = op.get_bind()
    matrices: dict = {}
    old = bind.execute(sa.text(f"SELECT id, {', '.join(OLD.values())} FROM products WHERE "
                               + " OR ".join(f"{c} IS NOT NULL" for c in OLD.values()))).mappings()
    for row in old:
        matrix = matrices.setdefault(row["id"], {})
        for currency, column in OLD.items():
            if row[column] is not None:
                matrix[currency] = {g: str(Decimal(row[column]).quantize(CENT)) for g in GROUPS}

    # The last applied worksheet row per product: its code, else a colour's SKU.
    applied = bind.execute(sa.text("""
        SELECT DISTINCT ON (p.id) p.id, r.sheet
        FROM pricing_records r
        JOIN products p ON lower(p.base_code) = lower(trim(r.product_code))
            OR p.id IN (SELECT v.product_id FROM product_variants v WHERE lower(v.sku) = lower(trim(r.product_code)))
        WHERE r.applied_at IS NOT NULL AND trim(r.product_code) <> ''
        ORDER BY p.id, (lower(p.base_code) = lower(trim(r.product_code))) DESC, r.applied_at DESC
    """)).mappings()
    for row in applied:
        sheet = row["sheet"] if isinstance(row["sheet"], dict) else json.loads(row["sheet"])
        erp = _sheet_erp(sheet)
        if erp:
            current = matrices.setdefault(row["id"], {}).setdefault(sheet.get("currency", "TRY"), {})
            for group, price in erp.items():
                current.setdefault(group, price)

    for product_id, matrix in matrices.items():
        bind.execute(
            sa.text("UPDATE products SET erp_prices = CAST(:m AS jsonb) WHERE id = :id"),
            {"m": json.dumps(matrix), "id": product_id},
        )
    for column in OLD.values():
        op.drop_column("products", column)


def downgrade() -> None:
    for column in OLD.values():
        op.add_column("products", sa.Column(column, sa.Numeric(12, 2), nullable=True))
    # One price per currency: the first group's, in the catalogue's order.
    for currency, column in OLD.items():
        op.execute(f"""
            UPDATE products SET {column} = COALESCE(
                {", ".join(f"(erp_prices->'{currency}'->>'{g}')::numeric" for g in GROUPS)})
            WHERE erp_prices ? '{currency}'
        """)
    op.drop_column("products", "erp_prices")
