"""product media gallery, YouTube link, and "Kömür Kuralı" → "Sepet Kuralı"

A product carries a gallery now, not one photo: `image_urls` holds them in order
and `image_url` keeps the cover, so everything that reads one photo (cards, cart
thumbnails, metadata) is untouched. Existing photos become single-item galleries.

The coal rule was renamed by the business to the cart rule ("Ağır Kargo rakamı
3.000 TL üzeri alışverişlerde sepetin %50'sini geçemez"), so its column follows.

Revision ID: 9c3e7a2b6d14
Revises: 8b2d5f1c4e07
Create Date: 2026-10-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '9c3e7a2b6d14'
down_revision: Union[str, Sequence[str], None] = '8b2d5f1c4e07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("products", sa.Column("image_urls", postgresql.JSONB(), nullable=True))
    op.add_column("products", sa.Column("youtube_video_url", sa.String(length=500), nullable=True))
    # The photo a product already has becomes the first (and only) gallery entry.
    op.execute(
        "UPDATE products SET image_urls = jsonb_build_array(image_url) WHERE image_url IS NOT NULL"
    )
    op.alter_column("products", "is_coal_rule_applied", new_column_name="is_cart_rule_applied")


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column("products", "is_cart_rule_applied", new_column_name="is_coal_rule_applied")
    op.drop_column("products", "youtube_video_url")
    op.drop_column("products", "image_urls")
