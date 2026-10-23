"""The two cards beside the hero get a photo of their own each

promo_boxes: top_card_image_url / bottom_card_image_url (none = the shop's
own photo, as until now) and each one's horizontal focus.

Revision ID: f1a7c3e9d264
Revises: e6b3d9f1a274
Create Date: 2026-10-23
"""

import sqlalchemy as sa
from alembic import op

revision = "f1a7c3e9d264"
down_revision = "e6b3d9f1a274"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("promo_boxes", sa.Column("top_card_image_url", sa.String(500), nullable=True))
    op.add_column("promo_boxes", sa.Column("top_card_focus_x", sa.SmallInteger(), nullable=False, server_default="50"))
    op.add_column("promo_boxes", sa.Column("bottom_card_image_url", sa.String(500), nullable=True))
    op.add_column("promo_boxes", sa.Column("bottom_card_focus_x", sa.SmallInteger(), nullable=False, server_default="50"))


def downgrade() -> None:
    for column in ("bottom_card_focus_x", "bottom_card_image_url", "top_card_focus_x", "top_card_image_url"):
        op.drop_column("promo_boxes", column)
