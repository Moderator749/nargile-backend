"""Access rules per customer ("Cari bazında")

Revision ID: e3b9d5f1a276
Revises: d1a7c3e9b524
Create Date: 2026-11-25 14:00:00.000000

customer_access_rules: categories, brands and storefront pages one customer
may not see, on top of their group's rules (tier_access_rules). New and
empty; removed with the account.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e3b9d5f1a276"
down_revision: Union[str, None] = "d1a7c3e9b524"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "customer_access_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_type", sa.String(16), nullable=False),
        sa.Column("target", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.UniqueConstraint("user_id", "target_type", "target", name="uq_customer_access_rule"),
    )
    op.create_index("ix_customer_access_rules_user_id", "customer_access_rules", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_customer_access_rules_user_id", table_name="customer_access_rules")
    op.drop_table("customer_access_rules")
