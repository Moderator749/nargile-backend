"""Promo box free layout (part positions)

Revision ID: b6e2f8a41d07
Revises: a3d7e5c19b42
Create Date: 2026-10-12 17:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b6e2f8a41d07"
down_revision: Union[str, None] = "a3d7e5c19b42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("promo_boxes", sa.Column("layout", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("promo_boxes", "layout")
