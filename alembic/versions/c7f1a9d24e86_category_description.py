"""categories.description: the category page's introduction

Revision ID: c7f1a9d24e86
Revises: b4e2c8f61d93
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op

revision = "c7f1a9d24e86"
down_revision = "b4e2c8f61d93"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("categories", sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("categories", "description")
