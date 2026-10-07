"""product cross-sells ("Birlikte Satılanlar")

Up to four products pinned to another product's page. A join table, so deleting a
product takes its references with it.

Revision ID: c7d2b4e85a31
Revises: b5e81a37c2f9
Create Date: 2026-10-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7d2b4e85a31'
down_revision: Union[str, Sequence[str], None] = 'b5e81a37c2f9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "product_cross_sells",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("product_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("related_product_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["related_product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_id", "related_product_id", name="uq_cross_sell_pair"),
        sa.CheckConstraint("product_id <> related_product_id", name="ck_cross_sell_not_self"),
    )
    op.create_index("ix_product_cross_sells_product", "product_cross_sells", ["product_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_product_cross_sells_product", table_name="product_cross_sells")
    op.drop_table("product_cross_sells")
