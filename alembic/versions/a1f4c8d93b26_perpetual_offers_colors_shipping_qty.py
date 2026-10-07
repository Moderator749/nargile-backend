"""perpetual offers, colour labels as a list, free-shipping quantity condition

- flash_sales.ends_at becomes nullable: an offer with no deadline runs until its
  opportunity stock runs out or the admin turns it off.
- products.color_name (one label) becomes products.color_names (a list), keeping
  whatever was already entered.
- products.free_shipping_min_quantity: "adet şartı" for free carriage.

Revision ID: a1f4c8d93b26
Revises: 9c3e7a2b6d14
Create Date: 2026-10-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a1f4c8d93b26'
down_revision: Union[str, Sequence[str], None] = '9c3e7a2b6d14'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column("flash_sales", "ends_at", existing_type=sa.DateTime(timezone=True), nullable=True)
    op.drop_constraint("ck_flash_sales_window", "flash_sales", type_="check")
    op.create_check_constraint("ck_flash_sales_window", "flash_sales", "ends_at IS NULL OR ends_at > starts_at")

    op.add_column("products", sa.Column("color_names", postgresql.JSONB(), nullable=True))
    op.execute(
        "UPDATE products SET color_names = jsonb_build_array(color_name) "
        "WHERE color_name IS NOT NULL AND btrim(color_name) <> ''"
    )
    op.drop_column("products", "color_name")
    op.add_column("products", sa.Column("free_shipping_min_quantity", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("products", "free_shipping_min_quantity")
    op.add_column("products", sa.Column("color_name", sa.String(length=60), nullable=True))
    op.execute("UPDATE products SET color_name = left(color_names->>0, 60) WHERE color_names IS NOT NULL")
    op.drop_column("products", "color_names")

    op.drop_constraint("ck_flash_sales_window", "flash_sales", type_="check")
    op.execute("DELETE FROM flash_sales WHERE ends_at IS NULL")
    op.create_check_constraint("ck_flash_sales_window", "flash_sales", "ends_at > starts_at")
    op.alter_column("flash_sales", "ends_at", existing_type=sa.DateTime(timezone=True), nullable=False)
