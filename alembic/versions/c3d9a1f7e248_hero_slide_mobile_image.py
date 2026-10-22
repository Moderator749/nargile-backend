"""Hero slides' own phone photo

The portrait photo a phone shows in place of the slide's wide one, kept with
the slide (and edited in its "Mobil" view) rather than in the homepage
document's "Mobil Yönetim", where none had been saved. Empty for every slide.

Revision ID: c3d9a1f7e248
Revises: b8f2e4a6c013
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from alembic import op

revision = "c3d9a1f7e248"
down_revision = "b8f2e4a6c013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hero_slides", sa.Column("mobile_image", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("hero_slides", "mobile_image")
