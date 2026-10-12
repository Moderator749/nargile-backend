"""Export order requests: a cart an international customer asks us to price

An INTERNATIONAL account has no payment screen. It builds a cart at export
prices and submits it as a request: nothing is charged, no stock is deducted,
and the freight is quoted afterwards against the transport type it chose.

Three changes carry that: a QUOTE payment method (an order that was never
paid for), ``orders.is_quote`` (so the sales figures can tell a request from a
sale), and ``orders.transport_mode`` (road, air or sea).

Revision ID: d8c1f5a60b72
Revises: c4a7e91d2b85
Create Date: 2026-10-10
"""

import sqlalchemy as sa
from alembic import op

revision = "d8c1f5a60b72"
down_revision = "c4a7e91d2b85"
branch_labels = None
depends_on = None

TRANSPORT_MODES = ("ROAD", "AIR", "SEA")


def upgrade() -> None:
    # A new enum value must be committed before rows can use it.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE paymentmethod ADD VALUE IF NOT EXISTS 'QUOTE'")

    transport = sa.Enum(*TRANSPORT_MODES, name="transportmode")
    transport.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "orders",
        sa.Column("is_quote", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "orders",
        sa.Column("transport_mode", transport, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "transport_mode")
    op.drop_column("orders", "is_quote")
    sa.Enum(name="transportmode").drop(op.get_bind(), checkfirst=True)
    # The QUOTE payment method stays: Postgres cannot drop one enum value, and
    # any order that used it would be left unreadable.
