"""Brands get a description and every brand name products carry; categories get
their own product order

- brands.description: a line or two for the brand's page.
- category_product_orders: the admin's order of a category page's products.
- Every brand name a product carries that the brands table does not know
  (by name, slug or search term, ignoring case) becomes a brand, active, at
  the end of the list — so no brand a product names is left without a page.

Revision ID: d5a2c8e4f193
Revises: c3d9a1f7e248
Create Date: 2026-10-23
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.slugs import slugify

revision = "d5a2c8e4f193"
down_revision = "c3d9a1f7e248"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("brands", sa.Column("description", sa.Text(), nullable=True))
    op.create_table(
        "category_product_orders",
        sa.Column("category_id", sa.Integer(), sa.ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True),
        sa.Column(
            "product_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("products.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("position", sa.Integer(), nullable=False),
    )
    op.create_index("ix_category_product_orders_position", "category_product_orders", ["category_id", "position"])

    conn = op.get_bind()
    known: set[str] = set()
    slugs: set[str] = set()
    last = 0
    for name, slug, term, order in conn.execute(sa.text("SELECT name, slug, search_term, sort_order FROM brands")):
        known |= {name.casefold(), slug.casefold(), term.casefold()}
        slugs.add(slug)
        last = max(last, order or 0)
    carried = conn.execute(
        sa.text("SELECT DISTINCT trim(brand) FROM products WHERE brand IS NOT NULL AND trim(brand) <> '' ORDER BY 1")
    ).scalars()
    for name in carried:
        if name.casefold() in known or slugify(name) in slugs or not slugify(name):
            continue
        slug, n = slugify(name), 2
        while slug in slugs:
            slug, n = f"{slugify(name)}-{n}", n + 1
        last += 10
        conn.execute(
            sa.text(
                "INSERT INTO brands (name, slug, search_term, is_active, sort_order) VALUES (:name, :slug, :term, true, :order)"
            ),
            {"name": name[:80], "slug": slug, "term": name.lower()[:100], "order": last},
        )
        known.add(name.casefold())
        slugs.add(slug)


def downgrade() -> None:
    op.drop_index("ix_category_product_orders_position", table_name="category_product_orders")
    op.drop_table("category_product_orders")
    op.drop_column("brands", "description")
