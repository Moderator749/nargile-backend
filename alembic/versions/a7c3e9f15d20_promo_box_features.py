"""Promo box: the advantages card's own feature lines

Revision ID: a7c3e9f15d20
Revises: f3b8d1c64a27
Create Date: 2026-11-17 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7c3e9f15d20"
down_revision: Union[str, None] = "f3b8d1c64a27"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("promo_boxes", sa.Column("features", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("promo_boxes", "features")
