"""Retire the CMS advantages and the promo card variants

The advantages are edited in "Ana Sayfa Düzenleme" (the "Avantajlar" tab,
site_content.homepage); the old `advantages` table behind the unlinked
/admin/advantages page fed nothing, and is dropped.

Of the promo boxes, only the "social" one (beside the hero) is still shown.
The other variants (image, text, transparent cards in two rows under the
slider) are deleted, and with them the columns only they used: row, variant,
the photo (upload or URL), its veil, focus and zoom, the "Şok" badge and the
free layout.

The downgrade brings the columns and the table back, empty: the rows deleted
here are not restored. Back them up before upgrading if they may be wanted.

Revision ID: a1c5e9f2d7b3
Revises: f3a8d2c61b09
Create Date: 2026-10-19
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a1c5e9f2d7b3"
down_revision = "f3a8d2c61b09"
branch_labels = None
depends_on = None

PROMO_CARD_COLUMNS = (
    "row", "variant", "image_path", "image_url", "badge", "layout",
    "overlay_direction", "image_focus_x", "image_focus_y", "image_zoom",
)


def upgrade() -> None:
    op.execute("DELETE FROM promo_boxes WHERE variant <> 'social'")
    for column in PROMO_CARD_COLUMNS:
        op.drop_column("promo_boxes", column)
    op.drop_table("advantages")


def downgrade() -> None:
    op.create_table(
        "advantages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("icon", sa.String(length=30), nullable=False),
        sa.Column("title", sa.String(length=80), nullable=False),
        sa.Column("highlight", sa.String(length=40), nullable=False),
        sa.Column("highlight_label", sa.String(length=120), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("footnote", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.add_column("promo_boxes", sa.Column("row", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("promo_boxes", sa.Column("variant", sa.String(length=20), nullable=False, server_default="social"))
    op.add_column("promo_boxes", sa.Column("image_path", sa.String(length=255), nullable=True))
    op.add_column("promo_boxes", sa.Column("image_url", sa.String(length=500), nullable=True))
    op.add_column("promo_boxes", sa.Column("badge", sa.String(length=20), nullable=True))
    op.add_column("promo_boxes", sa.Column("layout", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("promo_boxes", sa.Column("overlay_direction", sa.String(length=10), nullable=False, server_default="to-t"))
    op.add_column("promo_boxes", sa.Column("image_focus_x", sa.Integer(), nullable=False, server_default="50"))
    op.add_column("promo_boxes", sa.Column("image_focus_y", sa.Integer(), nullable=False, server_default="50"))
    op.add_column("promo_boxes", sa.Column("image_zoom", sa.Integer(), nullable=False, server_default="100"))
