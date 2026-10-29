"""Products belong to a brand: products.brand_id

A brand was a product's free-text field (products.brand), matched to the
brands table by its words — "Sultan" and "Sultan Nargile" were two spellings
of one maker, and a brand's page found its products by searching their names.
Each product now points at its Brand row; the text is kept, written as the
brand's own name, for what reads it as words (search, the product page).

Backfill: the text matched to a brand by its name, search term or slug
("Sultan" being Sultan Nargile's). Adalya is left without a brand at all, by
the shop's choice: no brand page under a tobacco maker's name. Sultan
Nargile's slug becomes "sultan", the address its page (today its category's)
has at the storefront's root.

Revision ID: b4d7e2a9c6f1
Revises: f2a8c5e1b934
Create Date: 2026-10-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b4d7e2a9c6f1"
down_revision: Union[str, None] = "f2a8c5e1b934"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("products", sa.Column("brand_id", sa.Integer(), sa.ForeignKey("brands.id", ondelete="SET NULL")))
    op.create_index("ix_products_brand_id", "products", ["brand_id"])
    op.execute("UPDATE products SET brand = NULL WHERE lower(trim(brand)) = 'adalya'")
    op.execute("UPDATE brands SET slug = 'sultan' WHERE slug = 'sultan-nargile' AND NOT EXISTS (SELECT 1 FROM brands WHERE slug = 'sultan')")
    op.execute(
        """
        UPDATE products p SET brand_id = b.id, brand = b.name
        FROM brands b
        WHERE p.brand IS NOT NULL AND trim(p.brand) <> ''
          AND (lower(trim(p.brand)) IN (lower(b.name), lower(b.search_term), lower(b.slug))
               OR (lower(trim(p.brand)) = 'sultan' AND b.slug = 'sultan'))
        """
    )


def downgrade() -> None:
    op.execute("UPDATE brands SET slug = 'sultan-nargile' WHERE slug = 'sultan'")
    op.drop_index("ix_products_brand_id", table_name="products")
    op.drop_column("products", "brand_id")
