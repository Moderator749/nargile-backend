"""Fiyat Hesaplama Cetveli: pricing files and worksheet rows

``pricing_files`` are the named folders ("Dosyalar"); ``pricing_records`` the
rows, each a whole worksheet as JSON. A row with no file is a draft
("Taslaklar"). Deleting a file deletes its rows; prices already written to
products are not touched.

Revision ID: d6b2e8f41a37
Revises: c3a9d2e85f14
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d6b2e8f41a37"
down_revision = "c3a9d2e85f14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pricing_files",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=120), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_table(
        "pricing_records",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("file_id", sa.Integer(), sa.ForeignKey("pricing_files.id", ondelete="CASCADE"), nullable=True),
        sa.Column("product_code", sa.String(length=60), nullable=False, server_default=""),
        sa.Column("product_name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("sheet", postgresql.JSONB(), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_summary", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_pricing_records_file_id", "pricing_records", ["file_id"])
    op.create_index("ix_pricing_records_created_at", "pricing_records", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_pricing_records_created_at", table_name="pricing_records")
    op.drop_index("ix_pricing_records_file_id", table_name="pricing_records")
    op.drop_table("pricing_records")
    op.drop_table("pricing_files")
