"""Session token version per user

Every session token carries the user's version; raising it (a password
reset does) ends every session issued before. Existing users start at 0,
which is what a token from before this carries too, so nobody is signed out.

Revision ID: f3a8d2c61b09
Revises: e5b1c7d24f80
Create Date: 2026-10-19
"""

import sqlalchemy as sa
from alembic import op

revision = "f3a8d2c61b09"
down_revision = "e5b1c7d24f80"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("users", "token_version")
