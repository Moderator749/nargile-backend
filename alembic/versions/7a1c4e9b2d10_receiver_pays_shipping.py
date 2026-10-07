"""receiver-pays shipping: who pays the carriage, and the buyer's consent

The shop no longer charges carriage on the order (shipping_cost is always 0).
Orders therefore have to record whether the shop or the receiver pays the
carrier, and when the buyer accepted paying it on delivery.

Revision ID: 7a1c4e9b2d10
Revises: 994b1cd2048d
Create Date: 2026-10-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7a1c4e9b2d10'
down_revision: Union[str, Sequence[str], None] = '994b1cd2048d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

payer = sa.Enum("SHOP", "RECEIVER", name="shippingpayer")


def upgrade() -> None:
    """Upgrade schema."""
    payer.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "orders",
        sa.Column("shipping_payer", payer, nullable=False, server_default="SHOP"),
    )
    op.add_column("orders", sa.Column("shipping_consent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("orders", "shipping_consent_at")
    op.drop_column("orders", "shipping_payer")
    payer.drop(op.get_bind(), checkfirst=True)
