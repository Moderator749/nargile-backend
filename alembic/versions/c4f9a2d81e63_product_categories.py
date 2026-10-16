"""Products in several categories: product_categories

Revision ID: c4f9a2d81e63
Revises: b8e3f17c4d92
Create Date: 2026-10-14

A product can now be listed under more than one category (MR.WOOD and Lüleler,
say). `products.category_id` stays as the primary one — the breadcrumb's — and
every product that has one gets it as its first row here.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "c4f9a2d81e63"
down_revision: Union[str, None] = "b8e3f17c4d92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "product_categories",
        sa.Column("product_id", UUID(as_uuid=True), sa.ForeignKey("products.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("category_id", sa.Integer(), sa.ForeignKey("categories.id"), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_product_categories_category", "product_categories", ["category_id"])
    op.execute(
        "INSERT INTO product_categories (product_id, category_id, position) "
        "SELECT id, category_id, 0 FROM products WHERE category_id IS NOT NULL"
    )


def downgrade() -> None:
    # The extra categories are lost; the primary one is still on the product.
    op.drop_index("ix_product_categories_category", table_name="product_categories")
    op.drop_table("product_categories")
