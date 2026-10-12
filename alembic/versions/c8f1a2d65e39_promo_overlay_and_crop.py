"""Promo box photo veil direction and crop

Revision ID: c8f1a2d65e39
Revises: b6e2f8a41d07
Create Date: 2026-10-12 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c8f1a2d65e39"
down_revision: Union[str, None] = "b6e2f8a41d07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("promo_boxes", sa.Column("overlay_direction", sa.String(length=10), nullable=False, server_default="to-t"))
    op.add_column("promo_boxes", sa.Column("image_focus_x", sa.Integer(), nullable=False, server_default="50"))
    op.add_column("promo_boxes", sa.Column("image_focus_y", sa.Integer(), nullable=False, server_default="50"))
    op.add_column("promo_boxes", sa.Column("image_zoom", sa.Integer(), nullable=False, server_default="100"))


def downgrade() -> None:
    op.drop_column("promo_boxes", "image_zoom")
    op.drop_column("promo_boxes", "image_focus_y")
    op.drop_column("promo_boxes", "image_focus_x")
    op.drop_column("promo_boxes", "overlay_direction")
