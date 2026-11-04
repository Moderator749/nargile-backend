"""Categories: a banner over the category page (``banner_url``).

A wide image (about 4:1) under the page's heading, uploaded per category in
the panel. Null = no banner: the page closes the space, nothing in its place.

Revision ID: d3a9e5c71f20
Revises: b8d4f2a6c1e3
Create Date: 2026-11-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d3a9e5c71f20"
down_revision: Union[str, None] = "b8d4f2a6c1e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("categories", sa.Column("banner_url", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("categories", "banner_url")
