"""products.slug: the product page's address, /product/<slug>

Existing rows get their name's slug (Turkish letters folded, "-2", "-3"… on a
clash, oldest product first, so the result is the same on every machine).
Products imported from the old store then take that store's own address with
scripts/apply_legacy_product_slugs.py.

Revision ID: a9d4e7c31f58
Revises: e3b8f1a25c07
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op

from app.core.slugs import product_slug_base

revision = "a9d4e7c31f58"
down_revision = "e3b8f1a25c07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("slug", sa.String(255), nullable=True))
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, name FROM products ORDER BY created_at, id")).all()
    taken: set[str] = set()
    for product_id, name in rows:
        base = product_slug_base(name or "")
        slug, n = base, 2
        while slug in taken:
            slug, n = f"{base}-{n}", n + 1
        taken.add(slug)
        conn.execute(sa.text("UPDATE products SET slug = :slug WHERE id = :id"), {"slug": slug, "id": product_id})
    op.alter_column("products", "slug", nullable=False)
    op.create_index("ix_products_slug", "products", ["slug"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_products_slug", table_name="products")
    op.drop_column("products", "slug")
