"""A deleted product's stock codes can be used again

Revision ID: c6e2a9f41d58
Revises: b4e1c7a9d352
Create Date: 2026-11-24 10:00:00.000000

A deleted product kept its colours active, and the stock code index covered
every colour, so its codes stayed taken for good: re-creating "MLZ-162-1"
after deleting it was refused. A deleted product's colours are now retired
with it (as a removed colour is), and the index covers colours on sale only,
case aside (every lookup by code already ignores case).

product_variants is small (about 1,400 rows): the update and the index build
take milliseconds; lock_timeout keeps a busy table from queueing reads.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c6e2a9f41d58"
down_revision: Union[str, None] = "b4e1c7a9d352"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "UPDATE product_variants v SET is_active = false FROM products p "
        "WHERE p.id = v.product_id AND p.is_deleted AND v.is_active"
    )
    op.drop_index("ix_product_variants_sku", table_name="product_variants")
    op.create_index(
        "ix_product_variants_sku",
        "product_variants",
        [sa.text("lower(sku)")],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    # Fails if a deleted product's code has been used again since: that is the
    # point of the upgrade, and two rows cannot both keep it under the old index.
    # Deleted products' colours stay retired (they are off sale either way).
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("ix_product_variants_sku", table_name="product_variants")
    op.create_index("ix_product_variants_sku", "product_variants", ["sku"], unique=True)
