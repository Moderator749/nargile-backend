"""Group access rules, and a product's retail kuruş switch

Revision ID: d1a7c3e9b524
Revises: c6e2a9f41d58
Create Date: 2026-11-25 12:00:00.000000

- tier_access_rules: "Grup Erişim Kısıtlamaları" — which customer group (or
  a guest) may not see which category, brand or storefront page. New and
  empty: nothing is hidden until an admin adds a rule.
- products.allow_retail_kurus: keep a product's retail TL price as typed
  instead of rounding it up to whole lira. A boolean with a constant default
  is a catalogue-only change in PostgreSQL 11+ (no table rewrite); the
  lock_timeout keeps a busy products table from queueing reads behind it.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d1a7c3e9b524"
down_revision: Union[str, None] = "c6e2a9f41d58"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "tier_access_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tier", sa.String(16), nullable=False),
        sa.Column("target_type", sa.String(16), nullable=False),
        sa.Column("target", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.UniqueConstraint("tier", "target_type", "target", name="uq_tier_access_rule"),
    )
    op.create_index("ix_tier_access_rules_tier", "tier_access_rules", ["tier"])
    op.add_column(
        "products",
        sa.Column("allow_retail_kurus", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("products", "allow_retail_kurus")
    op.drop_index("ix_tier_access_rules_tier", table_name="tier_access_rules")
    op.drop_table("tier_access_rules")
