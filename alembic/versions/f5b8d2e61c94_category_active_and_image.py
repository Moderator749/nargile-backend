"""Category active switch and cover image

Revision ID: f5b8d2e61c94
Revises: e2a6c9d14f73
Create Date: 2026-10-12 14:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f5b8d2e61c94"
down_revision: Union[str, None] = "e2a6c9d14f73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("categories", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("categories", sa.Column("image_url", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("categories", "image_url")
    op.drop_column("categories", "is_active")
