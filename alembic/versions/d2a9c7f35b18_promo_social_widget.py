"""Promo box social & advantages widget

Revision ID: d2a9c7f35b18
Revises: c8f1a2d65e39
Create Date: 2026-10-12 19:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d2a9c7f35b18"
down_revision: Union[str, None] = "c8f1a2d65e39"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("promo_boxes", sa.Column("widget", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("promo_boxes", "widget")
