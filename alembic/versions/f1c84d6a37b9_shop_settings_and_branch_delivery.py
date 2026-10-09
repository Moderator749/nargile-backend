"""Settings the admin panel can change, and branch delivery for a firm

``shop_settings`` is a key/value table for the numbers that used to be
constants. The first of them is the İstanbul limit: the subtotal from which the
shop's own van carries a firm's order for nothing ("İstanbul içi 20000 ve üzeri
alışverişlerde ücretsiz teslimat"). It is seeded with that default, so the
panel opens on the figure the shop has always used.

Orders gain ``shipping_branch_name``: "Şubeme istiyorum", the firm's own branch
an order goes to instead of its registered invoice address.

Revision ID: f1c84d6a37b9
Revises: e9a3c47b218f
Create Date: 2026-10-09
"""

import sqlalchemy as sa
from alembic import op

from app.services.shop_settings import ISTANBUL_FREE_MIN_DEFAULT, ISTANBUL_FREE_MIN_KEY

revision = "f1c84d6a37b9"
down_revision = "e9a3c47b218f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    settings = op.create_table(
        "shop_settings",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.String(length=200), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("updated_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.bulk_insert(
        settings,
        [{"key": ISTANBUL_FREE_MIN_KEY, "value": f"{ISTANBUL_FREE_MIN_DEFAULT}"}],
    )

    op.add_column("orders", sa.Column("shipping_branch_name", sa.String(length=150), nullable=True))


def downgrade() -> None:
    # Lossy: the saved limits go with the table, and so do the branch names.
    op.drop_column("orders", "shipping_branch_name")
    op.drop_table("shop_settings")
