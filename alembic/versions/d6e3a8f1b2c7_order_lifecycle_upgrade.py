"""Order lifecycle: delivered, returned, recovered, the shop's own note

- Two order statuses: DELIVERED (in the customer's hands — the carrier's
  delivery or "Mağazadan Teslim") and RETURNED (İade: it had left, and came
  back; unlike CANCELLED, which never left).
- A carrier value, STORE, for an order collected at the shop.
- orders.delivered_at, returned_at, return_reason, recovered_at ("Siparişi
  Kurtar": a cancelled order brought back) and internal_note ("Gizli Mağaza
  Notu", admin-only).

Backfill: a shipment Yurtiçi already reported delivered (carrier_status DLV)
becomes DELIVERED, at the time the sync last heard of it.

Revision ID: d6e3a8f1b2c7
Revises: b4d7e2a9c6f1
Create Date: 2026-10-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d6e3a8f1b2c7"
down_revision: Union[str, None] = "b4d7e2a9c6f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A new enum value must be committed before rows can use it.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'DELIVERED' AFTER 'SHIPPED'")
        op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'RETURNED' AFTER 'DELIVERED'")
        op.execute("ALTER TYPE carrier ADD VALUE IF NOT EXISTS 'STORE'")

    op.add_column("orders", sa.Column("recovered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("returned_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("return_reason", sa.String(length=300), nullable=True))
    op.add_column("orders", sa.Column("internal_note", sa.Text(), nullable=True))

    op.execute(
        "UPDATE orders SET status = 'DELIVERED', delivered_at = COALESCE(carrier_synced_at, shipped_at) "
        "WHERE status = 'SHIPPED' AND carrier_status = 'DLV'"
    )


def downgrade() -> None:
    # Postgres cannot drop an enum value: the statuses go back to the nearest
    # old one, and the values stay in the type unused.
    op.execute("UPDATE orders SET status = 'SHIPPED' WHERE status = 'DELIVERED'")
    op.execute("UPDATE orders SET status = 'CANCELLED', cancelled_at = returned_at, cancel_reason = return_reason WHERE status = 'RETURNED'")
    op.execute("UPDATE orders SET carrier = 'OWN' WHERE carrier = 'STORE'")
    op.drop_column("orders", "internal_note")
    op.drop_column("orders", "return_reason")
    op.drop_column("orders", "returned_at")
    op.drop_column("orders", "delivered_at")
    op.drop_column("orders", "recovered_at")
