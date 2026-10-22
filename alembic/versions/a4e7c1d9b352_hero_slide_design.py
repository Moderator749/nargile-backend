"""Hero slides' design: the dark wash on or off, and a size and colour per text

The wash is on for every slide that exists, as it always was; sizes and
colours are empty, so every slide keeps its own until the admin sets them.
(The button becoming optional needs no change: an empty label and link are
stored as "".)

Revision ID: a4e7c1d9b352
Revises: e5b9c2d4f810
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a4e7c1d9b352"
down_revision = "e5b9c2d4f810"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hero_slides", sa.Column("enable_overlay", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("hero_slides", sa.Column("font_sizes", postgresql.JSONB(), nullable=True))
    op.add_column("hero_slides", sa.Column("text_colors", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("hero_slides", "text_colors")
    op.drop_column("hero_slides", "font_sizes")
    op.drop_column("hero_slides", "enable_overlay")
