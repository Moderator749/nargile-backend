"""products.created_at / updated_at, for the sitemap's <lastmod>

Every existing row gets the moment the migration runs: the catalogue was
imported days ago and nothing older is known, which is also the truth as far
as a crawler is concerned.

Revision ID: e3b8f1a25c07
Revises: d7a4c2e86b19
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op

revision = "e3b8f1a25c07"
down_revision = "d7a4c2e86b19"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.add_column(
        "products",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_column("products", "updated_at")
    op.drop_column("products", "created_at")
