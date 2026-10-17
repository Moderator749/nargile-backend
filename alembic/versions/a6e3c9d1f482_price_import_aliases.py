"""Toplu fiyat: names from the pricing workbook tied to products by the admin

Revision ID: a6e3c9d1f482
Revises: d2a8e5b47c19
Create Date: 2026-10-17
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a6e3c9d1f482"
down_revision = "d2a8e5b47c19"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "price_import_aliases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=120), nullable=False, unique=True),
        sa.Column("excel_name", sa.String(length=120), nullable=False),
        sa.Column(
            "product_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("products.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
    )


def downgrade() -> None:
    op.drop_table("price_import_aliases")
