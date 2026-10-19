"""List today's "Fırsat Ürünü" products under the deals category

From now on the product sheet keeps a product in the deals category
(DEALS_CATEGORY_SLUG, "kampanyalar" — "Fırsat Ürünler") while its deal switch
is on. This puts the products whose switch is already on there too, at the
end of their categories, and makes it the primary of any that had none. It
adds only: nothing already in the category is taken out. Without such a
category it does nothing.

The downgrade leaves the links: they cannot be told from ones an admin made.

Revision ID: c7d3a8e1f925
Revises: a1c5e9f2d7b3
Create Date: 2026-10-19
"""

import sqlalchemy as sa
from alembic import op

revision = "c7d3a8e1f925"
down_revision = "a1c5e9f2d7b3"
branch_labels = None
depends_on = None

DEALS_SLUG = "kampanyalar"


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO product_categories (product_id, category_id, position)
            SELECT p.id, c.id,
                   COALESCE((SELECT MAX(pc.position) + 1 FROM product_categories pc WHERE pc.product_id = p.id), 0)
            FROM products p
            JOIN categories c ON c.slug = :slug
            WHERE p.is_opportunity
              AND NOT EXISTS (
                  SELECT 1 FROM product_categories x WHERE x.product_id = p.id AND x.category_id = c.id
              )
            """
        ).bindparams(slug=DEALS_SLUG)
    )
    op.execute(
        sa.text(
            """
            UPDATE products SET category_id = c.id
            FROM categories c
            WHERE c.slug = :slug AND products.is_opportunity AND products.category_id IS NULL
            """
        ).bindparams(slug=DEALS_SLUG)
    )


def downgrade() -> None:
    pass
