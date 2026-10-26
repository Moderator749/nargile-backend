"""The promo box is one box: every text editable, and a shade direction per card

promo_boxes.top_title (the community card's title) and bottom_text (the line
under the advantages card's title), which the storefront used to hard-code;
top_shade / bottom_shade, where each card's dark veil starts: "left" (the
look until now), "right", "top" or "bottom".

The panel now edits a single box. Where several are active, the most
recently created stays active and the others are switched off — not
deleted, so nothing is lost.

Revision ID: c7e2a9d4f013
Revises: b2d5f8a1c736
Create Date: 2026-10-26
"""

import sqlalchemy as sa
from alembic import op

revision = "c7e2a9d4f013"
down_revision = "b2d5f8a1c736"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("promo_boxes", sa.Column("top_title", sa.String(120), nullable=True))
    op.add_column("promo_boxes", sa.Column("bottom_text", sa.String(300), nullable=True))
    op.add_column("promo_boxes", sa.Column("top_shade", sa.String(6), nullable=False, server_default="left"))
    op.add_column("promo_boxes", sa.Column("bottom_shade", sa.String(6), nullable=False, server_default="left"))
    op.execute(
        "UPDATE promo_boxes SET is_active = false "
        "WHERE is_active AND id <> (SELECT max(id) FROM promo_boxes WHERE is_active)"
    )


def downgrade() -> None:
    # Which boxes were switched off is not recorded: they stay off.
    op.drop_column("promo_boxes", "bottom_shade")
    op.drop_column("promo_boxes", "top_shade")
    op.drop_column("promo_boxes", "bottom_text")
    op.drop_column("promo_boxes", "top_title")
