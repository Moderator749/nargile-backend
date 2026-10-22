"""Hero slides' mobile design: positions, sizes and photo crop of their own on a phone

Three JSON columns, empty for every slide that exists: a phone shows each
slide as it always has until the admin designs its mobile view.

Revision ID: b8f2e4a6c013
Revises: a4e7c1d9b352
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b8f2e4a6c013"
down_revision = "a4e7c1d9b352"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hero_slides", sa.Column("mobile_layout", postgresql.JSONB(), nullable=True))
    op.add_column("hero_slides", sa.Column("mobile_font_sizes", postgresql.JSONB(), nullable=True))
    op.add_column("hero_slides", sa.Column("mobile_bg_position", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("hero_slides", "mobile_bg_position")
    op.drop_column("hero_slides", "mobile_font_sizes")
    op.drop_column("hero_slides", "mobile_layout")
