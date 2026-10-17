"""Product rule "Bayide Havale / EFT İndirimi": the havale discount for dealers too

Off for every existing product: until now the discount has been a retail perk
only, and a dealer's tier price was costed without it.

Revision ID: b4d7e2a9c315
Revises: a6e3c9d1f482
Create Date: 2026-10-17
"""

import sqlalchemy as sa
from alembic import op

revision = "b4d7e2a9c315"
down_revision = "a6e3c9d1f482"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("has_dealer_havale_discount", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("products", "has_dealer_havale_discount")
