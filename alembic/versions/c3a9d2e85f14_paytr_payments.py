"""PayTR card payments: two order statuses and the payment attempts table

A card order now waits for PayTR (AWAITING_PAYMENT) and either becomes paid
(APPROVED, as before) or PAYMENT_FAILED, which releases what it held like a
cancellation. Each payment page opened is a row in ``payment_transactions``,
which is how PayTR's notification is matched back to its order.

Orders already placed by card are left as they are (PENDING_APPROVAL): they
were taken before there was a gateway, and an admin approves them as before.

Revision ID: c3a9d2e85f14
Revises: b8e1f4a62d07
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c3a9d2e85f14"
down_revision = "b8e1f4a62d07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A new enum value must be committed before rows can use it.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'AWAITING_PAYMENT' AFTER 'AWAITING_TRANSFER'")
        op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'PAYMENT_FAILED' AFTER 'CANCELLED'")

    op.create_table(
        "payment_transactions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("order_id", sa.UUID(), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False, server_default="PAYTR"),
        sa.Column("merchant_oid", sa.String(length=64), nullable=False, unique=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM("PENDING", "SUCCESS", "FAILED", name="paymentstatus"),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("test_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("total_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("payment_type", sa.String(length=20), nullable=True),
        sa.Column("failed_reason_code", sa.String(length=20), nullable=True),
        sa.Column("failed_reason_msg", sa.String(length=300), nullable=True),
        sa.Column("callback_payload", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_payment_transactions_order_id", "payment_transactions", ["order_id"])


def downgrade() -> None:
    # The two order statuses stay in the enum (Postgres cannot drop a value);
    # orders that used them are left as they are.
    op.drop_index("ix_payment_transactions_order_id", table_name="payment_transactions")
    op.drop_table("payment_transactions")
    op.execute("DROP TYPE IF EXISTS paymentstatus")
