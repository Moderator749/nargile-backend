"""Translations: products' and categories' text in English and Arabic

One JSON column on each, {"en": {...}, "ar": {...}} (app.schemas.i18n); the
Turkish stays where it is. Empty until a translation is written, so the
storefront shows the Turkish meanwhile.

Revision ID: e5b9c2d4f810
Revises: c7d3a8e1f925
Create Date: 2026-10-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e5b9c2d4f810"
down_revision = "c7d3a8e1f925"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("translations", postgresql.JSONB(), nullable=True))
    op.add_column("categories", sa.Column("translations", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("categories", "translations")
    op.drop_column("products", "translations")
