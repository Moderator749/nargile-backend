"""A category's own closing passage, in place of the shop's "Alt SEO Metni"

categories.seo_title, categories.seo_text: none = the shop's passage, as
until now.

Revision ID: a1c4e7f2b905
Revises: f1a7c3e9d264
Create Date: 2026-10-23
"""

import sqlalchemy as sa
from alembic import op

revision = "a1c4e7f2b905"
down_revision = "f1a7c3e9d264"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("categories", sa.Column("seo_title", sa.String(160), nullable=True))
    op.add_column("categories", sa.Column("seo_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("categories", "seo_text")
    op.drop_column("categories", "seo_title")
