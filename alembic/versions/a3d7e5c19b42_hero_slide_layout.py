"""Hero slide free layout (part positions)

Revision ID: a3d7e5c19b42
Revises: f5b8d2e61c94
Create Date: 2026-10-12 16:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a3d7e5c19b42"
down_revision: Union[str, None] = "f5b8d2e61c94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("hero_slides", sa.Column("layout", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("hero_slides", "layout")
