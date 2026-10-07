"""volume-discount rule, and its Fırsat override with the quantity condition

The product sheet grew a "Ciro İndirimi" rule, and the Fırsat block now overrides
both it and the free-carriage quantity condition while a deal runs. Both discount
flags default to true: the volume discount applies to everything today, so a
false default would quietly change what carts are worth.

Revision ID: b5e81a37c2f9
Revises: a1f4c8d93b26
Create Date: 2026-10-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b5e81a37c2f9'
down_revision: Union[str, Sequence[str], None] = 'a1f4c8d93b26'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("products", sa.Column("has_volume_discount", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column(
        "products", sa.Column("opp_has_volume_discount", sa.Boolean(), nullable=False, server_default=sa.true())
    )
    op.add_column("products", sa.Column("opp_free_shipping_min_quantity", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("products", "opp_free_shipping_min_quantity")
    op.drop_column("products", "opp_has_volume_discount")
    op.drop_column("products", "has_volume_discount")
