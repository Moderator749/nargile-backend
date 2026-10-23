"""Each hero slide's buttons have a size of their own

hero_slides.button_size_desktop / button_size_mobile: "sm", "md" (the shop's
own, every slide's until now) or "lg".

Revision ID: b2d5f8a1c736
Revises: a1c4e7f2b905
Create Date: 2026-10-23
"""

import sqlalchemy as sa
from alembic import op

revision = "b2d5f8a1c736"
down_revision = "a1c4e7f2b905"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hero_slides", sa.Column("button_size_desktop", sa.String(2), nullable=False, server_default="md"))
    op.add_column("hero_slides", sa.Column("button_size_mobile", sa.String(2), nullable=False, server_default="md"))


def downgrade() -> None:
    op.drop_column("hero_slides", "button_size_mobile")
    op.drop_column("hero_slides", "button_size_desktop")
