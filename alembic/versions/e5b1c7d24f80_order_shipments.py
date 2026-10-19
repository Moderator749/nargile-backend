"""Order shipments: the carrier, its tracking page, and Yurtiçi's web service

What "Kargola" records beyond the tracking code: who took the parcel and
when, the page the customer follows it on, and — for a shipment registered
with Yurtiçi's web service — our reference for it and the last status
Yurtiçi reported. Orders shipped before stay NULL.

Revision ID: e5b1c7d24f80
Revises: d2e8b4f61a93
Create Date: 2026-10-19
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e5b1c7d24f80"
down_revision = "d2e8b4f61a93"
branch_labels = None
depends_on = None

carrier = postgresql.ENUM("YURTICI", "OTHER", "OWN", name="carrier")


def upgrade() -> None:
    carrier.create(op.get_bind(), checkfirst=True)
    op.add_column("orders", sa.Column("carrier", postgresql.ENUM(name="carrier", create_type=False), nullable=True))
    op.add_column("orders", sa.Column("carrier_name", sa.String(100), nullable=True))
    op.add_column("orders", sa.Column("tracking_url", sa.String(300), nullable=True))
    op.add_column("orders", sa.Column("shipped_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("cargo_key", sa.String(30), nullable=True))
    op.add_column("orders", sa.Column("carrier_status", sa.String(10), nullable=True))
    op.add_column("orders", sa.Column("carrier_status_text", sa.String(200), nullable=True))
    op.add_column("orders", sa.Column("carrier_synced_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_orders_cargo_key", "orders", ["cargo_key"])


def downgrade() -> None:
    op.drop_index("ix_orders_cargo_key", table_name="orders")
    for column in (
        "carrier_synced_at", "carrier_status_text", "carrier_status", "cargo_key",
        "shipped_at", "tracking_url", "carrier_name", "carrier",
    ):
        op.drop_column("orders", column)
    carrier.drop(op.get_bind(), checkfirst=True)
