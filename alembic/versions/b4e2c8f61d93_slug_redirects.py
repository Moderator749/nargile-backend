"""slug_redirects: old product/category addresses, kept answering after a rename

Revision ID: b4e2c8f61d93
Revises: a9d4e7c31f58
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "b4e2c8f61d93"
down_revision = "a9d4e7c31f58"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "slug_redirects",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("old_slug", sa.String(255), nullable=False),
        sa.Column("product_id", UUID(as_uuid=True), sa.ForeignKey("products.id", ondelete="CASCADE")),
        sa.Column("category_id", sa.Integer(), sa.ForeignKey("categories.id", ondelete="CASCADE")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("kind", "old_slug", name="uq_slug_redirect"),
        sa.CheckConstraint("(product_id IS NULL) <> (category_id IS NULL)", name="ck_slug_redirect_one_target"),
    )


def downgrade() -> None:
    op.drop_table("slug_redirects")
