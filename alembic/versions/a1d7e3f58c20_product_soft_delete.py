"""Products: soft delete (``is_deleted``, ``deleted_at``).

A deleted product leaves the storefront and the admin lists but keeps its row,
so the order lines that point at its colours stay intact. Every existing
product starts not deleted.

Revision ID: a1d7e3f58c20
Revises: f4b8d2c6a913
Create Date: 2026-11-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1d7e3f58c20"
down_revision: Union[str, None] = "f4b8d2c6a913"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("products", sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("products", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_products_is_deleted", "products", ["is_deleted"])


def downgrade() -> None:
    op.drop_index("ix_products_is_deleted", table_name="products")
    op.drop_column("products", "deleted_at")
    op.drop_column("products", "is_deleted")
