"""Product rules "Toptanda / Kafede Havale / EFT İndirimi", beside the bayi one

Off for every existing product, as the bayi switch was: the havale discount has
been a retail perk, and a tier price was costed without it.

Revision ID: c9a1f5e3b724
Revises: b4d7e2a9c315
Create Date: 2026-10-18
"""

import sqlalchemy as sa
from alembic import op

revision = "c9a1f5e3b724"
down_revision = "b4d7e2a9c315"
branch_labels = None
depends_on = None

COLUMNS = ("has_toptan_havale_discount", "has_cafe_havale_discount")


def upgrade() -> None:
    for column in COLUMNS:
        op.add_column("products", sa.Column(column, sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    for column in COLUMNS:
        op.drop_column("products", column)
