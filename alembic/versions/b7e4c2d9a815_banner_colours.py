"""Banner colours: the text colour on the banner, and presets with their own look.

``site_announcements.text_color``; ``banner_presets.title``,
``background_style`` and ``text_color``. All null on existing rows: the
banner's text stays automatic, and every preset keeps drawing in the
banner's look until it is given one of its own.

Revision ID: b7e4c2d9a815
Revises: a1d7e3f58c20
Create Date: 2026-11-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7e4c2d9a815"
down_revision: Union[str, None] = "a1d7e3f58c20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("site_announcements", sa.Column("text_color", sa.String(7), nullable=True))
    op.add_column("banner_presets", sa.Column("title", sa.String(80), nullable=True))
    op.add_column("banner_presets", sa.Column("background_style", sa.String(32), nullable=True))
    op.add_column("banner_presets", sa.Column("text_color", sa.String(7), nullable=True))


def downgrade() -> None:
    op.drop_column("banner_presets", "text_color")
    op.drop_column("banner_presets", "background_style")
    op.drop_column("banner_presets", "title")
    op.drop_column("site_announcements", "text_color")
